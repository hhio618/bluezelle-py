import base64
import itertools
import json
import platform
import signal
from io import BytesIO

import requests
from google.protobuf import json_format
from google.protobuf.message import Message

from bluzelle.codec.tendermint.abci.types_pb2 import (Request, RequestCheckTx,
                                                      RequestInfo,
                                                      RequestQuery, Response,
                                                      ResponseException,
                                                      ResponseFlush,
                                                      ResponseInfo,
                                                      ResponseQuery)
from bluzelle.utils import *

MaxReadInBytes = 64 * 1024  # Max we'll consume on a read stream
AGENT = "bluzelle-py/0.1"

class Tendermint34Client:
    """Tendermint34Client is the transport to interacting with the bluzelle blockchain.

    it is responsible to querying the blockchain data using predefined bluzelle|cosmos
    grpc Message's as well as broadcasting the signed transaction to the blockchain 
    network.
    """
    def __init__(self, host: str, port: int):
        # Tendermint endpoint
        self.uri = "{}:{}".format(host, port)

        # Keep a session
        self.session = requests.Session()

        # Request counter for json-rpc
        self.request_counter = itertools.count()

        # request headers
        self.headers = {"user-agent": AGENT, "Content-Type": "application/json"}

    def __getattribute__(self, name):
        """Redirect extra calls to the pb_invoke method"""
        try:
            return object.__getattribute__(self, name)
        except AttributeError:
            return self.pb_invoke(name)

    def call(self, method, params):
        """Send json+rpc calls to the tendermint rpc.
        
        Args:
          method: the rpc method, usually a grpc method name.
          params: parmeters to send along the rpc rquest.
        
        Raised:
          ValueError: if there is an error response.
        """
        
        value = str(next(self.request_counter))
        encoded = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or [],
                "id": value,
            }
        )

        print("json+rpc input: ", encoded)
        
        # Sending the request.
        r = self.session.post(self.uri, data=encoded, headers=self.headers, timeout=3)

        # Check for status errors.
        try:
            r.raise_for_status()
        except Exception as er:
            raise er

        response = r.content

        if is_string(response):
            result = json.loads(bytes_to_str(response))
        if "error" in result:
            raise ValueError(result["error"])
        
        print("json+rpc response: ", result)

        # Check if there is a (code, log) within inner object.
        result = result["result"]
        inner = result
        if "response" in inner:
            inner = result["response"]
        if "code" in inner and inner["code"] != 0:
            raise ValueError(inner["log"])
        
        return result

    @property
    def is_connected(self):
        """Check if we are still connected"""
        try:
            response = self.status()
        except IOError:
            return False
        else:
            assert response["node_info"]
            return True
        assert False

    def _send_transaction(self, name, tx):
        return self.call(name, {"tx": list(tx.SerializeToString())})

    def broadcast_tx_sync(self, tx):
        """Broadcasting the grpc signed Tx message using the tendermint rpc.
        Args: 
          tx: bytes data of signed cosmos grpc Tx message.
        """
        return self._send_transaction("broadcast_tx_sync", tx)

    def tx_search(self, query: str, prove: bool = True, page: int = None, per_page: int = None):
        """Searching tx data using an input query."""
        req = {
            "query": query,
            "prove": prove,
            "page": page,
            "per_page": per_page,
        }
        return self.call("tx_search", req)

    def abci_query(self, path: str, data: str, height: int = None, prove: bool = False):
        """Query the blockchain data using standard blockchain abci.
        
        Args:
          path: usually fully quilified name of a grpc Message.
          data: the Message data.
          height: the blockchain height to run the query against.
          prove: boolean, default to false.
        """
        req = RequestQuery(path=path, data=data, height=height, prove=prove)
        return self.pb_invoke("abci_query")(req)

    def abci_info(self):
        return self.pb_invoke("abci_info")(RequestInfo())

    def status(self):
        """Will be used to query the blockchain general informations like chain id, ..."""
        return self.call("status", [])

    def pb_invoke(self, method_name) -> bytes:
        """Converting predefined grpc Message's to a payload and make the rpc call."""
        def wrapper(req: Message):
            payload = json_format.MessageToDict(req)
            if method_name == "abci_query":
                payload["data"] = base64.b64decode(payload["data"]).hex()
            result = self.call(method_name, payload)
            return result["response"]["value"]

        return wrapper