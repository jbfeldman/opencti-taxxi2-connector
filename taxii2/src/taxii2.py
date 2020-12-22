"""Generic TAXII2 connector. """

import os
import time
import json

from datetime import datetime, timedelta
import yaml
from requests.exceptions import HTTPError

from taxii2client.v20 import (
    Server,
    ApiRoot
    )
from taxii2client.exceptions import TAXIIServiceException

from pycti import OpenCTIConnectorHelper, get_config_variable



class Taxii2Connector:
    """Connector object"""
    def __init__(self):
        """Read in config variables"""

        config_file_path = os.path.dirname(os.path.abspath(__file__)) + "/config.yml"
        config = (
            yaml.load(open(config_file_path), Loader=yaml.FullLoader)
            if os.path.isfile(config_file_path)
            else {}
        )
        self.helper = OpenCTIConnectorHelper(config)
        # Extra config
        self.create_report = get_config_variable(
            "TAXII2_FUTURE_CREATE_REPORTS", ["feed", "create_reports"], config
        )
        self.username = get_config_variable(
            'TAXII2_USERNAME', ["taxii2", "username"], config
        )
        self.password = get_config_variable(
            'TAXII2_PASSWORD', ["taxii2", "password"], config
        )
        self.is_v21 = get_config_variable(
            'TAXII2_V21', ["taxii2", "v2.1"], config
        )
        if self.is_v21:
            global Server, ApiRoot
            from taxii2client.v21 import (
                Server,
                ApiRoot
                )

        self.server_url = get_config_variable(
            "TAXII2_SERVER_URL", ["taxii2", "server_url"], config
        )
        discovery_tail = 'taxii/' if not self.is_v21 else 'taxii2/'
        self.discovery_url = os.path.join(self.server_url, discovery_tail)

        self.collections = get_config_variable(
            'TAXII2_COLLECTIONS', ["taxii2", "collections"], config
        ).split(',')

        self.initial_history = get_config_variable(
            'TAXII2_INITIAL_HISTORY', ["taxii2", "initial_history"], config, True
        )

        self.per_request = get_config_variable(
            'TAXII2_PER_REQUEST', ["taxii2", "per_request"], config, True
        )

        self.interval = get_config_variable(
            "TAXII2_INTERVAl", ["taxii2", "interval"], config, True
        )

        self.update_existing_data = get_config_variable(
            "CONNECTOR_UPDATE_EXISTING_DATA",
            ["connector", "update_existing_data"],
            config,
        )
        #In a crisis, smash glass and uncomment this line of code
        #self.helper.config['uri'] = self.helper.config['uri'].replace('rabbitmq', '172.19.0.6')
    @staticmethod
    def _init_collection_table(colls):
        """
        Creates a table of string:Set where the key is the API root
        and the value is the list of Collections to read
        """
        table = {}
        for c in colls.split(','):
            root, coll = c.split('.')
            if root in table:
                table[root].add(coll)
            else:
                table[root] = {coll}
        print(table)
        return table


    def get_interval(self):
        """Converts interval hours to seconds"""
        return int(self.interval) * 3600

    @property
    def first_run(self):
        """Checks if connector has run before"""
        current_state = self.helper.get_state()
        return current_state is None or "last_run" not in current_state

    def run(self):
        """Run connector on a schedule"""
        while True:
            timestamp = int(time.time())
            if self.first_run:
                last_run = None
                self.helper.log_info("Connector has never run")

            else:
                last_run = datetime.utcfromtimestamp(
                    self.helper.get_state()["last_run"]
                ).strftime("%Y-%m-%d %H:%M:%S")
                self.helper.log_info("Connector last run: " + last_run)

            for collection in self.collections:
                try:
                    root_title, coll_title = collection.split('.')
                    if root_title == '*':
                        self.poll_all_roots(coll_title)
                    elif coll_title == '*':
                        self.poll_entire_root(root_title)
                    else:
                        url = os.path.join(self.server_url, root_title)
                        root = ApiRoot(url, user=self.username, password=self.password)
                        self.poll(root, coll_title)
                except (TAXIIServiceException, HTTPError) as err:
                    self.helper.log_error('Error connecting to TAXII server')
                    self.helper.log_error(err)
                    continue
            self.helper.log_info(f'Run Complete. Sleeping until next run in {self.interval} hours')
            self.helper.set_state({"last_run": timestamp})
            time.sleep(self.get_interval())

    def poll_all_roots(self, coll_title):
        """Polls all API roots for the specified collections"""
        server = Server(self.discovery_url, user=self.username, password=self.password)
        for root in server.api_roots:
            if coll_title == '*':
                #TODO: not sure root.name is right syntax
                self.poll_entire_root(root.title)
            else:
                try:
                    self.poll(root.title, coll_title)
                except TAXIIServiceException as err:
                    msg = f'Error trying to poll Collection {coll_title} in API Root {root.title}. Skipping'
                    self.helper.log_error(msg)
                    self.helper.log_error(e)

    def poll_entire_root(self, root_title, conn=None):
        """Polls all Collections in a given API Root"""

        url = os.path.join(self.server_url, root_title)
        root = ApiRoot(url, user=self.username, password=self.password, conn=conn)
        for coll in root.collections:
            self.poll(root, coll.title)

    def poll(self, root, coll_title, conn=None):
        """Polls a specified collection in a specified API root"""
        coll = self._get_collection(root, coll_title)

        #TODO: maybe function me out
        filters = {}
        if self.first_run:
            lookback = self.initial_history or None
        else:
            lookback = self.interval
        if lookback:
            added_after = datetime.now() - timedelta(hours=lookback)
            filters['added_after'] = added_after
        self.helper.log_info(f'Polling Collection {coll_title} in API Root {root.title}')
        self.send_to_server(coll.get_objects(**filters))

    def send_to_server(self, bundle):
        """Sends a STIX2 bundle to OpenCTI Server"""

        self.helper.log_info(f'Sending Bundle to server with {len(bundle["objects"])} objects')
        try:
            with open(f"{bundle['id']}.json", 'w') as bfile:
                json.dump(bundle, bfile, indent=2)
            # self.helper.send_stix2_bundle(
            #     json.dumps(bundle), update=self.update_existing_data,
            # )
        except Exception as e:
            self.helper.log_error(str(e))

    def _get_collection(self, root, coll_title):
        """Returns a COllection object, given an API Root and a collection name"""
        for coll in root.collections:
            if coll.title == coll_title:
                return coll
        msg = f'Collection {coll_title} does not exist in API root {root.title}'
        raise TAXIIServiceException(msg)



if __name__ == "__main__":
    try:
        connector = Taxii2Connector()
        connector.run()
    except Exception as e:
        raise(e)
        # print(e)
        # time.sleep(10)
        # exit(0)
