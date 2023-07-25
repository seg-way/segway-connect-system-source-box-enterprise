import json
import time
import asyncio
import orjson
import os

from boxsdk import Client, JWTAuth
from boxsdk.exception import BoxException
from boxsdk.object.events import EnterpriseEventsStreamType
import requests
import backoff

from syslogng import LogSource
from syslogng import LogMessage
from syslogng import Logger
from syslogng import Persist

logger = Logger()

config_path = os.environ.get("SEGWAY_BOX_SECRET_PATH", "")
    
class EventStream(LogSource):
    """Provides a syslog-ng async source for Microsoft Event hub"""

    cancelled: bool = False
    
    
    def init(self, options):
        self._client = None
        self.auth()
        logger.info("Authentication complete")
        self.persist = Persist("EventStream", defaults={"stream_position": 0})
        logger.info(f"Resuming collection at stream_position={self.persist}")        
        return True
    
    def auth(self):
            path = os.path.join(config_path,'box.json')
            f = open(path)
            self.auth_dict = json.load(f)
            f.close()
            try:
                result=JWTAuth.from_settings_dictionary(self.auth_dict)
            except (TypeError, ValueError, KeyError):
                logger.error('Could not load JWT from settings dictionary')
                return False
            self._client = Client(result)

    
    def run(self):
        """Simple Run method to create the loop"""
        asyncio.run(self.receive_batch())

    async def receive_batch(self):
        params = {
                    'limit': self._MAX_CHUNK_SIZE,
                    'stream_type': EnterpriseEventsStreamType.ADMIN_LOGS,
                    'stream_position': self.persist['stream_position']
                }
        timeout=5
        while not self.cancelled:
            # self.cancelled = True
            # try:
            box_response = self._get_events(params)
            events = EventStream.clean_event(box_response)
            for event in box_response:
                record_lmsg = LogMessage(event)
                self.post_message(record_lmsg)

            if box_response['next_stream_position'] and int(box_response['next_stream_position'])>0:
                self.persist['stream_position'] = box_response['next_stream_position']
                params['stream_position']=box_response['next_stream_position']
                logger.info(f"Posted count={len(events)} next_stream_position={params['stream_position']}")

    def backoff_hdlr(details):
        logger.info("Backing off {wait:0.1f} seconds after {tries} tries "
            "calling function {target} with args {args} and kwargs "
            "{kwargs}".format(**details))        
                                        
    @backoff.on_exception(backoff.expo,
                    (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError),max_time=300,on_backoff=backoff_hdlr)
    @backoff.on_predicate(backoff.expo, lambda x: x['entries'] == [],max_time=300,on_backoff=backoff_hdlr)
    def _get_events(self,params):
        box_response = self._client.make_request(
            'GET',
            self._client.get_url('events'),
            params=params,
            timeout=30
        )
        result = box_response.json()
        return result 
    
    

    @staticmethod
    def clean_event(source_dict: dict):
        """
        Delete keys with the value ``None``  or ```` (empty) string in a dictionary, recursively.
        Remove empty list and dict objects

        This alters the input so you may wish to ``copy`` the dict first.
        """
        # For Python 3, write `list(d.items())`; `d.items()` won’t work
        # For Python 2, write `d.items()`; `d.iteritems()` won’t work
        for key, value in list(source_dict.items()):
            if value is None:
                del source_dict[key]
            elif isinstance(value, str) and value in ("", "None", "none"):
                del source_dict[key]
            elif isinstance(value, str):
                if value.endswith("\n"):
                    value = value.strip("\n")

                if value.startswith('{"'):
                    try:
                        value = orjson.loads(value)
                        EventStream.clean_event(value)
                        source_dict[key] = value
                    except orjson.JSONDecodeError:
                        pass
            elif isinstance(value, dict) and not value:
                del source_dict[key]
            elif isinstance(value, dict):
                EventStream.clean_event(value)
            elif isinstance(value, list) and not value:
                del source_dict[key]
        return source_dict  # For convenience