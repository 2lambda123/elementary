from collections import defaultdict
from dataclasses import dataclass

import json
from datetime import datetime
from enum import Enum
from typing import Generic, List, Optional, TypeVar, Union

from elementary.clients.slack.schema import SlackMessageSchema
from elementary.monitor.alerts.alert import Alert, SlackAlertMessageBuilder
from elementary.monitor.alerts.malformed import MalformedAlert
from elementary.monitor.alerts.model import ModelAlert
from elementary.monitor.alerts.source_freshness import SourceFreshnessAlert
from elementary.monitor.alerts.test import ElementaryTestAlert, TestAlert
from elementary.utils.json_utils import try_load_json

AlertType = TypeVar("AlertType")


@dataclass
class AlertsQueryResult(Generic[AlertType]):
    alerts: List[AlertType]
    malformed_alerts: List[MalformedAlert]
    alerts_to_skip: Optional[List[Union[AlertType, MalformedAlert]]] = None

    @property
    def count(self) -> int:
        return len(self.alerts) + len(self.malformed_alerts)

    def get_all(self) -> List[Alert]:
        return self.alerts + self.malformed_alerts

    def get_alerts_to_skip(self) -> List[Optional[Union[AlertType, MalformedAlert]]]:
        return self.alerts_to_skip or []


@dataclass
class Alerts:
    tests: AlertsQueryResult[TestAlert]
    models: AlertsQueryResult[ModelAlert]
    source_freshnesses: AlertsQueryResult[SourceFreshnessAlert]

    @property
    def count(self) -> int:
        return self.models.count + self.tests.count + self.source_freshnesses.count

    @property
    def malformed_count(self):
        return (
                len(self.models.malformed_alerts)
                + len(self.tests.malformed_alerts)
                + len(self.source_freshnesses.malformed_alerts)
        )

    def get_all(self) -> List[Alert]:
        return (
                self.models.get_all()
                + self.tests.get_all()
                + self.source_freshnesses.get_all()
        )

    def get_elementary_test_count(self):
        elementary_test_count = defaultdict(int)
        for test_result in self.tests.alerts:
            if isinstance(test_result, ElementaryTestAlert):
                elementary_test_count[test_result.test_name] += 1
        return elementary_test_count


class GroupingType(Enum):
    BY_ALERT = "by_alert"
    BY_TABLE = "by_table"
    ALL = "all"


class GroupOfAlerts:
    # alerts: List[Alert]
    # grouping_type: GroupingType
    # channel_destination: str
    # owners: List[str]
    # subscribers: List[str]
    # errors: List[Alert]
    # warnings: List[Alert]
    # failures: List[Alert]

    def __init__(self,
                 alerts: List[Alert],
                 grouping_type: GroupingType,
                 default_channel_destination: str):

        self.alerts = alerts
        self.grouping_type = grouping_type
        self.slack_message_builder = SlackAlertMessageBuilder()

        # sort out model unique id - for groupby table:
        if self.grouping_type == GroupingType.BY_TABLE:
            models = set([al.model_unique_id for al in alerts])
            if len(models) != 1:
                raise ValueError(
                    f"failed initializing a GroupOfAlerts grouped by table, for alerts with mutliple models: {list(models)}")

        # sort out dest_channels: we get the default value, but if we have one other channel configured we switch to it.
        dest_channels = set([alert.slack_channel for alert in self.alerts])
        dest_channels.remove(None)  # no point in counting them, and no point in sending to a None channel
        if len(dest_channels) > 1:
            raise ValueError( #TODO don't merge without changing in here.
                f"Failed initializing a Group of Alerts with alerts that has different slack channel dest: {list(dest_channels)}")
        if len(dest_channels) == 1:
            self.channel_destination = list(dest_channels)[0]
        else:
            self.channel_destination = default_channel_destination

        # sort out errors / warnings / failures
        self.errors = []
        self.warnings = []
        self.failures = []
        for alert in self.alerts:
            if isinstance(alert, ModelAlert) or alert.status == "error":
                self.errors.append(alert)
            elif alert.status == "warn":
                self.warnings.append(alert)
            else:
                self.failures.append(alert)

        # sort out owners and subscribers and tags
        owners = set([])
        subscribers = set([])
        tags = set([])
        for al in self.alerts:
            if al.owners is not None:
                if isinstance(al.owners, list):
                    owners.update(al.owners)
                else:  # it's a string. could be comma delimited.
                    owners.update(al.owners.split(","))
            if al.subscribers is not None:
                if isinstance(al.subscribers, list):
                    subscribers.update(al.subscribers)
                else:  # it's a string. could be comma delimited.
                    subscribers.update(al.subscribers.split(","))
            if al.tags is not None:
                if isinstance(al.tags, str):
                    tags_unjsoned = try_load_json(al.tags)  # tags is a string, comma delimited values
                    if tags_unjsoned is None:  # maybe a string, maybe some comma delimited strings
                        tags.update(al.tags.split(","))
                    elif isinstance(tags_unjsoned, str):  # tags was a quoted string.
                        tags.update(al.tags.split(","))
                    elif isinstance(tags_unjsoned, list):  # tags was a list of strings
                        tags.update(tags_unjsoned)
                elif isinstance(al.tags, list):
                    tags.update(al.tags)

        import pdb; pdb.set_trace()
        self.owners = list(owners)
        self.subscribers = list(subscribers)
        TAG_PREFIX = "#"
        formatted_tags = [
            tag if tag.startswith(TAG_PREFIX) else f"{TAG_PREFIX}{tag}"
            for tag in tags
        ]

        self.tags = ", ".join(formatted_tags)

    def to_slack(self, is_slack_workflow=False):
        if self.grouping_type == GroupingType.BY_ALERT:
            return self.alerts[0].to_slack()

        # title, number of passed or failed,
        title_block = self._title_block()
        number_of_failed_error_block = self._number_of_failed_block()
        self.slack_message_builder._add_title_to_slack_alert(
            title_blocks=[title_block,
                          number_of_failed_error_block])

        # attention required : tags, owners, subscribers
        attention_required_blocks = self._attention_required_blocks()
        self.slack_message_builder._add_preview_to_slack_alert(preview_blocks=attention_required_blocks)

        details_blocks = []
        # failed, warning, error blocks.
        titles = [":X: *Failed tests*", ":warning: *Warning*", ":exclamation: *Error*"]
        empty_messages = ["_No Failures_", "_No Warnings_", "_No Errors_"]
        for al_list, title, empty_message in zip([self.failures, self.warnings, self.errors], titles, empty_messages):
            details_blocks.append(self.slack_message_builder.create_text_section_block(title))
            details_blocks.append(self.slack_message_builder.create_divider_block())
            if len(al_list) == 0:
                details_blocks.append(self.slack_message_builder.create_text_section_block(empty_message))
            else:
                details_blocks.append(self.slack_message_builder.create_text_section_block(self._tabulate_list_of_alerts(al_list)))
        self.slack_message_builder._add_blocks_as_attachments(details_blocks)

        return self.slack_message_builder.get_slack_message()

    def _title_block(self):
        if self.grouping_type == GroupingType.BY_TABLE:
            return self.slack_message_builder.create_header_block(
                f":small_red_triangle_down: {self.alerts[0].model_unique_id} ({len(self.alerts)} alerts)")
        elif self.grouping_type == GroupingType.ALL:
            return self.slack_message_builder.create_header_block(
                f":small_red_triangle_down: Alerts summary ({len(self.alerts)} alerts)")

    def _number_of_failed_block(self):
        # small_red_triangle: Falied: 36    |    :Warning: Warning: 3    |    :exclamation: Errors: 1
        return self.slack_message_builder.create_context_block(
            [
                f":small_red_triangle: Failed: {len(self.failures)}    |",
                f":Warning: Warning: {len(self.warnings)}    |",
                f":exclamation: Errors: {len(self.errors)}",
            ]
        )

    def _attention_required_blocks(self):
        tags_text = "_No Tags_" if not self.tags else self.tags
        owners_text = "_No Owners_" if not self.owners else ", ".join(self.owners)
        subscribers_text = "_No Subscribers_" if not self.subscribers else ", ".join(self.subscribers)
        preview_blocks = [
            self.slack_message_builder.create_text_section_block(":mega: *Attention required* :mega:"),
            self.slack_message_builder.create_text_section_block(f"*Tags:* {tags_text}"),
            self.slack_message_builder.create_text_section_block(f"*Owners:* {owners_text}"),
            self.slack_message_builder.create_text_section_block(f"*Subscribers:* {subscribers_text}"),
            self.slack_message_builder.create_empty_section_block(),
        ]
        return preview_blocks

    def _tabulate_list_of_alerts(self, al_list):
        def alert_to_concise_name(alert):
            if isinstance(alert, TestAlert):
                return f"{alert.test_type} - {alert.test_sub_type}"
            elif isinstance(alert, SourceFreshnessAlert):
                return f"source freshness alert"
            elif isinstance(alert, ModelAlert):
                return f"model alert"

        ret = []
        if self.grouping_type == GroupingType.ALL:  # group alerts by all --> list of table_name | test_name
            for al in al_list:
                ret.append(f"{al.model_unique_id} | {alert_to_concise_name(al)}")
        elif self.grouping_type == GroupingType.BY_TABLE:
            for al in al_list:
                ret.append(f"{alert_to_concise_name(al)} | {al.detected_at}")
        return "\n".join(ret)

# TODO if we want to not have "\n".join but seperate blocks, we can just do self.slack_message_builder.create_text_section_block() for each part.


class SlackMessageBuilder:
    pass

class SlackMessageThatInvolveMultipleRunResultsBuider(SlackMessageBuilder)

class ReportSummarySlackMessageBuilder(SlackMessageThatInvolveMultipleRunResultsBuider):
    pass

class GeneralGroupOfAlerts(SlackMessageThatInvolveMultipleRunResultsBuider):
    pass
class ByTableGroupOfAlert(GeneralGroupOfAlerts)
    pass

class ByAllGroupOfAlert(GeneralGroupOfAlerts)
    pass



"""
Stuff to test:
- business logic of getting the config
    - mock some test_meta and model_meta 
- business logic of _group_alerts_per_config 
- manually play a bit with overriding configs in the project level
- 


"""


















