import asyncio
import base64
import itertools
import json

from google.protobuf import json_format
from google.protobuf.message import Message
from jsonrpcclient.clients.websockets_client import WebSocketsClient
import requests
import websockets

from bluzelle.codec.tendermint.abci.types_pb2 import RequestInfo, RequestQuery
from bluzelle.utils import bytes_to_str, is_string

MaxReadInBytes = 64 * 1024  # Max we'll consume on a read stream
AGENT = "bluzelle-py/0.1"


class Tendermint34Client:
    """Tendermint34Client is the transport to interacting with the bluzelle
    blockchain.

    it is responsible to querying the blockchain data using predefined
    bluzelle|cosmos grpc Message's as well as broadcasting the signed
    transaction to the blockchain network.
    """

    def __init__(self, host: str, port: int):
        # Tendermint endpoint
        self.uri = "{}:{}".format(host, port)

        # Keep a session
        self.session = requests.Session()

        # keep the asyncio event loop
        self.loop = None

        # Request counter for json-rpc
        self.request_counter = itertools.count()

        # request headers
        self.headers = {"user-agent": AGENT, "Content-Type": "application/json"}

    def __getattribute__(self, name):
        """Redirect extra calls to the pb_invoke method."""
        try:
            return object.__getattribute__(self, name)
        except AttributeError:
            return self.pb_invoke(name, None)

    async def async_call(self, method, params):
        value = str(next(self.request_counter))
        encoded = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or [],
                "id": value,
            }
        )
        async with websockets.connect(self.uri + "/websocket") as ws:
            response = await WebSocketsClient(ws).send(encoded)
        return response

    async def call(self, method, params):
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
        print("json+rpc input: \n", encoded)

        if "wss" in self.uri or "ws" in self.uri:

            response = await self.async_call(method, params)
            print(f"response is {response.text}")
            result = json.loads(response.text)["result"]
            if "error" in result or "panic" in result:
                raise ValueError(result["error"])
        else:
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
            print(
                ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"
            )
            print("json+rpc response: ", result)
            print(
                "<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<"
            )

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
        """Check if we are still connected."""
        try:
            response = self.status()
        except IOError:
            if self.loop:
                self.loop.close()
            return False
        else:
            if response["node_info"] is None:
                if self.loop:
                    self.loop.close()
                return False
            return True

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

    async def abci_query(self, path: str, data: str, height: int = None, prove: bool = False):
        """Query the blockchain data using standard blockchain abci.

        Args:
          path: usually fully qualified name of a grpc Message.
          data: the Message data.
          height: the blockchain height to run the query against.
          prove: boolean, default to false.
        """
        req = RequestQuery(path=path, data=data, height=height, prove=prove)
        print(f"req is : {req}")
        res = await self.pb_invoke("abci_query", req)
        print(f"res is : {res}")
        return res

    def abci_info(self):
        return self.pb_invoke("abci_info", RequestInfo())

    def status(self):
        """Will be used to query the blockchain general information like chain
        id, ..."""
        return self.call("status", [])

    async def pb_invoke(self, method_name, req: Message or None) -> bytes:
        """Converting predefined grpc Message's to a payload and make the rpc
        call."""

        payload = json_format.MessageToDict(req)
        if method_name == "abci_query":
            payload["data"] = base64.b64decode(payload["data"]).hex()
        result = await self.call(method_name, payload)
        return result["response"]["value"]

