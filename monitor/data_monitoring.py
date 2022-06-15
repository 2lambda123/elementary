import os
from monitor.alerts import Alert
from monitor.dbt_runner import DbtRunner
from config.config import Config
from utils.log import get_logger
from utils.time import get_days_diff_from_now
from utils.json_utils import try_load_json
import json
from alive_progress import alive_it
from typing import Union, Optional

logger = get_logger(__name__)
FILE_DIR = os.path.dirname(__file__)


class DataMonitoring(object):
    DBT_PACKAGE_NAME = 'elementary'
    DBT_PROJECT_PATH = os.path.join(FILE_DIR, 'dbt_project')
    DBT_PROJECT_MODELS_PATH = os.path.join(FILE_DIR, 'dbt_project', 'models')
    # Compatibility for previous dbt versions
    DBT_PROJECT_MODULES_PATH = os.path.join(DBT_PROJECT_PATH, 'dbt_modules', DBT_PACKAGE_NAME)
    DBT_PROJECT_PACKAGES_PATH = os.path.join(DBT_PROJECT_PATH, 'dbt_packages', DBT_PACKAGE_NAME)

    def __init__(self, config: Config, slack_webhook: Union[str, None] = None) -> None:
        self.config = config
        self.dbt_runner = DbtRunner(self.DBT_PROJECT_PATH, self.config.profiles_dir)
        self.execution_properties = {}
        self.slack_webhook = slack_webhook or self.config.slack_notification_webhook

    def _dbt_package_exists(self) -> bool:
        return os.path.exists(self.DBT_PROJECT_PACKAGES_PATH) or os.path.exists(self.DBT_PROJECT_MODULES_PATH)

    @staticmethod
    def _split_list_to_chunks(items: list, chunk_size: int = 50) -> [list]:
        chunk_list = []
        for i in range(0, len(items), chunk_size):
            chunk_list.append(items[i: i + chunk_size])
        return chunk_list

    def _update_sent_alerts(self, alert_ids) -> None:
        alert_ids_chunks = self._split_list_to_chunks(alert_ids)
        for alert_ids_chunk in alert_ids_chunks:
            self.dbt_runner.run_operation(macro_name='update_sent_alerts',
                                          macro_args={'alert_ids': alert_ids_chunk},
                                          json_logs=False)

    def _query_alerts(self, days_back: int) -> list:
        json_alert_rows = self.dbt_runner.run_operation(macro_name='get_new_alerts',
                                                        macro_args={'days_back': days_back})
        self.execution_properties['alert_rows'] = len(json_alert_rows)
        alerts = []
        for json_alert_row in json_alert_rows:
            alert_row = json.loads(json_alert_row)
            alerts.append(Alert.create_alert_from_row(alert_row))
        return alerts

    def _send_to_slack(self, alerts: [Alert]) -> None:
        if self.slack_webhook is not None:
            sent_alerts = []
            alerts_with_progress_bar = alive_it(alerts, title="Sending alerts")
            for alert in alerts_with_progress_bar:
                alert.send_to_slack(self.slack_webhook, self.config.is_slack_workflow)
                sent_alerts.append(alert.id)

            sent_alert_count = len(sent_alerts)
            self.execution_properties['sent_alert_count'] = sent_alert_count
            if sent_alert_count > 0:
                pass
                #self._update_sent_alerts(sent_alerts)
        else:
            logger.info("Alerts found but slack webhook is not configured (see documentation on how to configure "
                        "a slack webhook)")

    def _download_dbt_package_if_needed(self, force_update_dbt_packages: bool):
        internal_dbt_package_exists = self._dbt_package_exists()
        self.execution_properties['dbt_package_exists'] = internal_dbt_package_exists
        self.execution_properties['force_update_dbt_packages'] = force_update_dbt_packages
        if not internal_dbt_package_exists or force_update_dbt_packages:
            logger.info("Downloading edr internal dbt package")
            package_downloaded = self.dbt_runner.deps()
            self.execution_properties['package_downloaded'] = package_downloaded
            if not package_downloaded:
                logger.info('Could not download internal dbt package')
                return

    def _send_alerts(self, days_back: int):
        alerts = self._query_alerts(days_back)
        alert_count = len(alerts)
        self.execution_properties['alert_count'] = alert_count
        alerts_and_totals = {}
        if alert_count > 0:
            alerts_dict = {}
            totals_dict = {}
            for alert in alerts:
                model_unique_id = alert.model_unique_id
                if model_unique_id in alerts_dict:
                    alerts_dict[model_unique_id].append(alert.to_dict())
                else:
                    alerts_dict[model_unique_id] = [alert.to_dict()]

                alert_days_diff = get_days_diff_from_now(alert.get_detection_time_utc())
                total_keys = []
                if alert_days_diff < 1:
                    total_keys.append('1d')
                if alert_days_diff < 7:
                    total_keys.append('7d')
                if alert_days_diff < 30:
                    total_keys.append('30d')
                if model_unique_id not in totals_dict:
                    totals_dict[model_unique_id] = {'1d': {'errors': 0, 'warnings': 0, 'resolved': 0},
                                                    '7d': {'errors': 0, 'warnings': 0, 'resolved': 0},
                                                    '30d': {'errors': 0, 'warnings': 0, 'resolved': 0}}
                if alert.status == 'warn':
                    totals_status = 'warnings'
                elif alert.status == 'error' or alert.status == 'fail':
                    totals_status = 'errors'
                else:
                    totals_status = None
                if totals_status is not None:
                    for key in total_keys:
                        totals_dict[model_unique_id][key][totals_status] += 1

            with open(os.path.join(self.config.target_dir, 'test_results.json'), 'w') as test_results_file:
                json.dump(alerts_dict, test_results_file)

            with open(os.path.join(self.config.target_dir, 'totals.json'), 'w') as totals_file:
                json.dump(totals_dict, totals_file)

            alerts_and_totals.update({'test_results': alerts_dict, 'totals': totals_dict})
        return alerts_and_totals
            #self._send_to_slack(alerts)

    def _read_configuration_to_sources_file(self) -> bool:
        logger.info("Reading configuration and writing to sources.yml")
        sources_yml = self.dbt_runner.run_operation(macro_name='read_configuration_to_sources_yml')
        if sources_yml is not None:
            if not os.path.exists(self.DBT_PROJECT_MODELS_PATH):
                os.makedirs(self.DBT_PROJECT_MODELS_PATH)
            sources_file_path = os.path.join(self.DBT_PROJECT_MODELS_PATH, 'sources.yml')
            with open(sources_file_path, 'w') as sources_file:
                sources_file.write(sources_yml)
            return True
        return False

    def run(self, days_back: int, force_update_dbt_package: bool = False, dbt_full_refresh: bool = False,
            alerts_only: bool = True) -> None:

        self._download_dbt_package_if_needed(force_update_dbt_package)

        if not alerts_only:
            success = self._read_configuration_to_sources_file()
            if not success:
                logger.info('Could not create configuration successfully')
                return

            logger.info("Running internal dbt run to create metadata and process configuration")
            success = self.dbt_runner.run(full_refresh=dbt_full_refresh)
            self.execution_properties['run_success'] = success
            if not success:
                logger.info('Could not run dbt run successfully')
                return

            logger.info("Running internal dbt data tests to collect metrics and calculate anomalies")
            success = self.dbt_runner.test(select="tag:elementary")
            self.execution_properties['test_success'] = success

        logger.info("Running internal dbt run to aggregate alerts")
        success = self.dbt_runner.run(models='alerts', full_refresh=dbt_full_refresh)
        self.execution_properties['alerts_run_success'] = success
        if not success:
            logger.info('Could not aggregate alerts successfully')
            return

    def generate_report(self, force_update_dbt_package: bool = False):

        self._download_dbt_package_if_needed(force_update_dbt_package)

        elementary_output = {}
        models, dbt_sidebar = self._get_dbt_models_and_sidebar()
        elementary_output['models'] = models
        elementary_output['dbt_sidebar'] = dbt_sidebar

        alerts_and_totals = self._send_alerts(7)
        elementary_output.update(alerts_and_totals)
        import webbrowser
        with open(os.path.join(FILE_DIR, 'index.html'), 'r') as index_html_file:
            html_code = index_html_file.read()
            elementary_output_str = json.dumps(elementary_output)
            elementary_output_html = f"""
                    {html_code}
                    <script>
                        var elementaryData = {elementary_output_str}
                    </script>
                """
            elementary_html_file_path = os.path.join(self.config.target_dir, 'elementary.html')
            with open(elementary_html_file_path, 'w') as elementary_output_html_file:
                elementary_output_html_file.write(elementary_output_html)
            with open(os.path.join(self.config.target_dir, 'elementary_output.json'), 'w') as \
                    elementary_output_json_file:
                elementary_output_json_file.write(elementary_output_str)

            elementary_html_file_path = 'file://' + elementary_html_file_path
            webbrowser.open_new_tab(elementary_html_file_path)

    def _get_dbt_models_and_sidebar(self):
        models = {}
        dbt_sidebar = {}
        results = self.dbt_runner.run_operation(macro_name='get_models')
        if results:
            model_dicts = json.loads(results[0])
            for model_dict in model_dicts:
                model_unique_id = model_dict.get('unique_id')
                self._normalize_dbt_model_dict(model_dict)
                models[model_unique_id] = model_dict
                self._update_dbt_sidebar(dbt_sidebar, model_unique_id, model_dict.get('normalized_full_path'),
                                         model_dict.get('package_name'))

        results = self.dbt_runner.run_operation(macro_name='get_sources')
        if results:
            source_dicts = json.loads(results[0])
            for source_dict in source_dicts:
                source_unique_id = source_dict.get('unique_id')
                self._normalize_dbt_model_dict(source_dict, is_source=True)
                models[source_unique_id] = source_dict
                self._update_dbt_sidebar(dbt_sidebar, source_unique_id, source_dict.get('normalized_full_path'),
                                         source_dict.get('package_name'))
        return models, dbt_sidebar

    @staticmethod
    def _update_dbt_sidebar(dbt_sidebar: dict, model_unique_id: str, model_full_path: str,
                            model_package_name: Optional[str]) -> None:
        if model_unique_id is None or model_full_path is None:
            return
        model_full_path_split = model_full_path.split(os.path.sep)
        if model_package_name and model_full_path_split:
            model_full_path_split.insert(0, model_package_name)
        for part in model_full_path_split:
            if part.endswith('.sql'):
                if 'files' in dbt_sidebar:
                    if model_unique_id not in dbt_sidebar['files']:
                        dbt_sidebar['files'].append(model_unique_id)
                else:
                    dbt_sidebar['files'] = [model_unique_id]
            else:
                if part not in dbt_sidebar:
                    dbt_sidebar[part] = {}
                dbt_sidebar = dbt_sidebar[part]

    @staticmethod
    def _normalize_dbt_model_dict(model: dict, is_source: bool = False) -> None:
        model_full_path = model.get('full_path')
        model_full_path_split = model_full_path.split(os.path.sep)
        file_name = None
        if model_full_path and model_full_path_split:
            file_name = model_full_path_split[-1]
        model['file_name'] = file_name
        owners = model.get('owners')
        loaded_owners = try_load_json(owners)
        if loaded_owners:
            owners = loaded_owners
        tags = model.get('tags')
        loaded_tags = try_load_json(tags)
        if loaded_tags:
            tags = loaded_tags
        model['owners'] = owners
        model['tags'] = tags
        model_name = model.get('name')
        model['model_name'] = model_name
        model['normalized_full_path'] = model_full_path
        if is_source:
            if model_full_path_split[0] == 'models':
                model_full_path_split[0] = 'sources'
            if model_full_path_split[-1].endswith('.yml'):
                model_full_path_split[-1] = model_name + '.sql'
            model['normalized_full_path'] = os.path.sep.join(model_full_path_split)

    def properties(self):
        data_monitoring_properties = {'data_monitoring_properties': self.execution_properties}
        return data_monitoring_properties



