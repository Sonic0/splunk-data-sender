# MIT License
#
# Copyright (c) 2020 Andrea Salvatori
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import gzip
import json
import logging
import socket
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from requests.exceptions import Timeout, ConnectionError, TooManyRedirects, RequestException, RetryError


log = logging.getLogger("Splunk-Data-Sender")
logging.basicConfig(format="%(name)s - %(asctime)s - %(levelname)s: - %(message)s")


class SplunkSender:
    """
    A logging handler to send events to a Splunk Enterprise instance
    running the Splunk HTTP Event Collector.
    """

    def __init__(self, endpoint, token, protocol='https', port='8088', source="Splunk data sender", hostname=None,
                 source_type='generic_single_line', allow_overrides=False, api_url='collector/event',
                 api_version=None, index='main', channel=None, channel_in='url', proxies=None, verify=True, timeout=30,
                 retry_count=5, retry_backoff=2.0, enable_debug=False, compress=True, max_buf_size=0,
                 event_formatter=None):
        """
        Args:
            endpoint (str): The Splunk Service endpoint param.
            token (str): Authentication token.
            protocol (str): The web protocol to use. Default 'https'.
            port (int): The port the Splunk Service endpoint is listening on.
            source (str): The Splunk source param.
            hostname (str): The Splunk Enterprise hostname.
            source_type (str): The Splunk source_type param. Defaults Non-Log file types.
                              https://docs.splunk.com/Documentation/Splunk/8.0.5/Data/Listofpretrainedsourcetypes
            allow_overrides (bool): Whether to look for one of the plunk built-in parameters(source, host, ecc)
            api_url (str): The HTTP Event Collector REST API endpoint.
                           https://docs.splunk.com/Documentation/Splunk/8.0.5/Data/HECRESTendpoints
            api_version (str): Protocol version for future scalability. No default version. Refer to the API docs.
            index (str): Splunk index to write to.
            channel (str): GUID. Required if useAck config is enabled in Splunk HEC instance.
            channel_in (str): Where pass channel. "header"("x-splunk-request-channel") or "url".
            proxies (list): The proxies to use for the request.
            verify (bool): Whether to perform SSL certificate validation.
            timeout (float): The time to wait for a response from Splunk. It is referred to each request.
            retry_count (int): The number of times to retry a failed request.
            retry_backoff (float): The requests lib backoff factor.
            enable_debug (bool): Whether to print debug console messages.
            compress (bool): Compress data with gzip before sending them to Splunk.
            max_buf_size (int): The number of events to keep in a buffer before sending them to Splunk.
                                0, the default, disables the feature.
            event_formatter (func): If set, this function will be called once for each events, before sending
                                    them to splunk. It will be responsible to reformat the event.
        """

        self.endpoint = endpoint
        self.token = token
        self.protocol = protocol if protocol in ("http", "https") else "https"
        self.port = port
        self.source = source
        self.hostname = hostname or socket.gethostname()
        self.allow_overrides = allow_overrides
        self.source_type = source_type
        self.api_url = api_url
        self.api_version = api_version or ''
        self.index = index
        self.channel = channel
        self.channel_in = channel_in if channel_in in ("url", "header") else "url"
        self.proxies = proxies
        self.verify = verify  # requests SSL verify. Making unverified HTTPS requests is strongly discouraged
        self.timeout = timeout
        self.session = requests.Session()
        self.retry_count = retry_count
        self.retry_backoff = retry_backoff
        self.debug = enable_debug
        self.__compress = compress
        self.__buf = []
        self.max_buf_size = max_buf_size
        self.__event_formatter = event_formatter

        # If severity level is INFO, the logger will handle only INFO, WARNING, ERROR, and CRITICAL messages
        #   and will ignore DEBUG messages.
        log_level = logging.DEBUG if self.debug else logging.INFO
        log.setLevel(log_level)
        log.debug("Starting in Debug Mode")

        # disable all warnings from urllib3 package
        if not self.verify:
            requests.packages.urllib3.disable_warnings()

        if self.verify and self.protocol == 'http':
            log.error('You should not use SSL Verify and unsecured connection')

        # set proxy to requests session
        if self.proxies:
            self.session.proxies = self.proxies

        if self.api_version and self.api_url == "collector":
            log.error("/collector api does not support versioning")
            raise ValueError("/collector api does not support versioning")

        # https://docs.splunk.com/Documentation/Splunk/8.0.5/Data/FormateventsforHTTPEventCollector
        if self.source_type == "_json" and self.api_url in ("collector/raw", f"collector/raw/{self.api_version}"):
            log.error("cannot send json record as raw data")
            raise ValueError("Json input must be sent either to the /collector or /collector/event endpoints")
        # https://docs.splunk.com/Documentation/Splunk/8.0.5/Data/IFXandHEC
        elif self.source_type == "_json" and self.api_url != "collector/event":
            log.warning("Requests containing the fields property must send to /collector/event endpoint. "
                        "Otherwise, they will not be indexed.")

        # Set up automatic retry with back-off
        log.debug("Preparing to create a Requests session")
        retry = Retry(total=self.retry_count,
                      backoff_factor=self.retry_backoff,
                      allowed_methods=False,  # Retry for any HTTP verb
                      status_forcelist=[500, 502, 503, 504],
                      redirect=1)
        self.session.mount(f"{self.protocol}://", HTTPAdapter(max_retries=retry))

        log.debug("Class initialize complete")

    def get_health(self):
        """
        This endpoint checks if HEC is healthy and able to accept new data from a load balancer.
        HEC health is determined if there is space available in the queue.
        https://docs.splunk.com/Documentation/Splunk/8.0.5/RESTREF/RESTinput#services.2Fcollector.2Fhealth

        Returns:
            bool: The return message from API call.
        """
        log.debug("send_health() called")
        # True response body like {"text": "HEC is healthy", "code": 17}
        splunk_response = self._get_from_splunk('get-health')
        # Splunk api doc does not specify the response payload and the means of response code inside it
        is_healthy, message = self._dispatch_splunk_health_res(splunk_response.status_code)
        if not is_healthy:
            log.error(message)
        else:
            log.info(message)

        return is_healthy

    def send_data(self, records):
        """
        Send events to HTTP Event Collector using the Splunk platform JSON event protocol.
        Events may be hold in a buffer depending on SplunkSender.max_buf_size.
        https://docs.splunk.com/Documentation/Splunk/8.0.5/RESTREF/RESTinput#services.2Fcollector.2Fevent

        Args:
            records (list): The logs data from the user.

        Returns:
            None: if buffering is enabled and not enough event are being hold
            dict: if buffering is disable or enough event are being hold, an API call is performed
                  and its return message is returned. Response body like {"text":"Success","code":0,"ackId":0}
        """
        log.debug("send_data() called")

        if not isinstance(records, list):
            tmp_list = list()
            tmp_list.append(records)
            records = tmp_list.copy()

        self.__buf += records

        if len(self.__buf) >= self.max_buf_size:
            return self.__send_data()

    def send_acks(self, acks):
        """
        Query event indexing status. For events sent using HTTP Event Collector, check event indexing status.
        Requests must use a valid channel ID and authorization token with useACK enabled.
        An event ACK ID, returned in response to a POST to services/collector, is also required.
        https://docs.splunk.com/Documentation/Splunk/8.0.5/RESTREF/RESTinput#services.2Fcollector.2Fack

        Args:
            acks (list/str): The acks to test the correct data processing

        Returns:
            dict: The return message from API call. response body like {"acks":{"0":true,"1":true, ...}}
        """
        log.debug("send_acks() called")
        if not isinstance(acks, list):
            tmp_list = list()
            tmp_list.append(acks)
            acks = tmp_list.copy()

        payload = json.dumps({"acks": acks})
        splunk_acks_response = self._send_to_splunk('send-ack', payload)
        return json.loads(splunk_acks_response.text)

    def flush_buffer(self):
        """
        Flush any events left in the buffer and send them to Splunk.
        You should call this method at the end of your program,
        to avoid losing events.

        Returns:
            None: if the buffer is empty as no API calls is made.
            dict: The return message from API call. response body like {"acks":{"0":true,"1":true, ...}}
        """
        if len(self.__buf) != 0:
            return self.__send_data()

    ##################
    # helper methods #
    ##################
    def __send_data(self):
        if len(self.__buf) == 0:
            return None

        payload = ""
        for record in self.__buf:
            try:
                formatted_record = self._format_record(record)
                payload = ''.join([payload, formatted_record])
            except Exception as err:
                log.error(f"Exception: {str(err)}")
                raise Exception from err
        self.__buf = []

        splunk_response = self._send_to_splunk('send-event', payload)
        return json.loads(splunk_response.text)

    def _format_record(self, record):
        log.debug("_format_record() called")

        params = {
            'time': self._get_splunk_attr(record, 'time', time.time()),
            'host': self._get_splunk_attr(record, 'host', self.hostname),
            'source': self._get_splunk_attr(record, 'source', self.source),
            'sourcetype': self._get_splunk_attr(record, 'sourcetype', self.source_type),
            'index': self._get_splunk_attr(record, 'index', self.index),
        }

        event = self._get_splunk_attr(record, 'event', record)
        params['event'] = self.__event_formatter(event) if self.__event_formatter else event

        if self.source_type == "_json" and isinstance(record, dict):
            params.update({'fields': record})

        log.debug("Record dictionary created")

        formatted_record = json.dumps(params, sort_keys=True)
        log.debug("Record formatting complete")

        return formatted_record

    def _get_splunk_attr(self, obj, attr, default=None):
        log.debug("_get_splunk_attr() called")
        val = default
        if isinstance(obj, dict) and self.allow_overrides:
            val = obj.get(attr, default)
            try:
                del obj[attr]
            except KeyError:
                log.warning(f"Using default value for {attr}")
        return val

    def _send_to_splunk(self, action, payload=None):
        log.debug("_send_to_splunk() called")
        if not payload and action != 'get-health':
            log.error("No payload provided")

        url, headers = self._dispatch_url_headers(action)

        log.debug(f"Destination URL is {url}")
        try:
            log.debug("Sending payload: " + payload)
            if self.__compress is True:
                log.debug("Gzipping payload")
                headers['Content-Encoding'] = 'gzip'
                payload = payload.encode('utf-8')
                payload = gzip.compress(payload)

            splunk_response = self.session.post(
                url,
                data=payload,
                headers=headers,
                verify=self.verify,
                timeout=self.timeout
            )
            self._check_splunk_response(splunk_response)
        except (Timeout, ConnectionError, TooManyRedirects, RequestException, RetryError) as err:
            log.error(f'POST, Max retries exceeded with url: {url}. Timeout or Connection refused')
            raise err
        else:
            return splunk_response

    def _get_from_splunk(self, action):
        log.debug("_get_from_splunk() called")

        url, headers = self._dispatch_url_headers(action)

        log.debug(f"Destination URL is {url}")
        try:
            splunk_response = self.session.get(
                url,
                headers=headers,
                verify=self.verify,
                timeout=self.timeout
            )
            self._check_splunk_response(splunk_response)
        except (Timeout, ConnectionError, TooManyRedirects, RequestException, RetryError) as err:
            log.error(f'GET, Max retries exceeded with url: {url}. Timeout or Connection refused')
            raise err
        else:
            return splunk_response

    def _check_splunk_response(self, splunk_response):
        splunk_res_code = json.loads(splunk_response.text).get('code')

        # HTTP 2XX = everything went as planned
        if 200 <= splunk_response.status_code <= 299:
            if 'acks' in splunk_response.text:
                log.info("Splunk ack response arrived")
            return

        # code is only present in case of send data, not in ack check.
        # If the code present, the http request has done correctly.
        if splunk_res_code or splunk_res_code == 0:  # 0 is False, so double check
            splunk_api_res_msg = self._dispatch_splunk_res_code(splunk_response.status_code, splunk_res_code)
            # TODO response from acks check does not have the "code"
            msg = f"Splunk response: -code: {splunk_res_code}, -HTTPcode: {splunk_response.status_code}, " \
                  f"-message: {splunk_api_res_msg}"
            if 200 <= splunk_response.status_code <= 299 and splunk_res_code == 0:
                log.info(msg)
            else:
                log.error(msg)
        else:
            log.warning("Response does not come directly from Splunk")

        splunk_response.raise_for_status()  # Throws exception for 4xx/5xx status

    def _dispatch_url_headers(self, action):
        """ Dispatch the correct header and url for the requested action
        Doc at --> https://docs.splunk.com/Documentation/Splunk/8.0.5/RESTREF/RESTinput#services.2Fcollector.2Fhealth

        Params:
            - action: (string) Three possibilities -> "send-health", "send-event" or "send-ack"
        """
        base_url = f"{self.protocol}://{self.endpoint}:{self.port}/services"

        suffix_url = {
            'get-health': "collector/health",
            'send-event': f"{self.api_url}",
            'send-ack': "collector/ack",
        }.get(action)

        if not suffix_url:
            raise ValueError("Action not implemented")

        url = f"{base_url}/{suffix_url}"
        # Add api version. send-ack action has not versioned api
        if self.api_version and action != "send-ack":
            url = f"{url}/{self.api_version}"

        headers = {'Authorization': f"Splunk {self.token}"}
        # Add Channel. "health" check api does not want the channel id
        if self.channel and action != "send-health":
            if self.channel_in == "url":
                url = f"{url}?channel={self.channel}"
                log.debug("Added channel to the url")
            else:  # channel_in == header
                headers.update({'x-splunk-request-channel': self.channel})
                log.debug("Added channel inside the header")

        return url, headers

    @staticmethod
    def _dispatch_splunk_res_code(HTTP_code, splunk_code):
        return {
            200: {0: 'Success'},
            400: {5: 'No data', 6: 'Invalid data format', 7: 'Incorrect index', 10: 'Data channel is missing',
                  11: 'Invalid data channel', 12: 'Event field is required', 13: 'Event field cannot be blank',
                  14: 'ACK is disabled', 15: 'Error in handling indexed fields',
                  16: 'Query string authorization is not enabled'},
            401: {2: 'Token is required', 3: 'Invalid authorization'},
            403: {1: 'Token disabled',
                  4: 'Invalid token'},
            500: {8: 'Internal server error'},
            503: {9: 'Server is busy'},
        }[HTTP_code].get(splunk_code, 'Not a valid Splunk Error')

    @staticmethod
    def _dispatch_splunk_health_res(HTTP_code):
        message = {
            200: 'HEC is available and accepting input',
            400: 'Invalid HEC token',
            503: 'HEC is unhealthy, queues are full',
        }.get(HTTP_code)

        if HTTP_code == 200:
            return True, message
        else:
            return False, message
