from __future__ import annotations
import asyncio
import json
import traceback
from typing import Awaitable, Callable, Dict, List, Tuple
from itertools import count
from collections import defaultdict

from websockets.server import WebSocketServerProtocol
from websockets.exceptions import ConnectionClosed
from chatroom import logger
from chatroom.topic_change import Change, SetChange

def make_message(message_type,**kwargs)->str:
    return json.dumps({"type":message_type,"args":kwargs})

def parse_message(message_json)->Tuple[str,dict]:
    message = json.loads(message_json)
    return message["type"],message["args"]

class Client:
    def __init__(self,id,ws:WebSocketServerProtocol,logger,sending_queue:asyncio.Queue[Tuple[Client,Tuple,Dict]]):
        self.id = id
        self._ws = ws
        self._logger = logger
        self._sending_queue = sending_queue

    async def _send_raw(self,message):
        await self._ws.send(message)
        self._logger.debug(f"<{self.id} {message}")

    async def send_async(self,*args,**kwargs):
        await self._send_raw(make_message(*args,**kwargs))

    def send(self,*args,**kwargs):
        self._sending_queue.put_nowait((self,args,kwargs))

    

class ClientManager:
    def __init__(self,GetTopicValue) -> None:
        self._get_topic_value = GetTopicValue
        self._logger = logger.Logger(logger.DEBUG,"CM")
        self._clients:Dict[int,Client] = {}
        self._client_id_count = count(1)
        self._message_handlers:Dict[str,Callable[...,None|Awaitable[None]]] = {'subscribe':self._handle_subscribe}
        self._subscriptions:defaultdict[str,set] =defaultdict(set)
        self._sending_queue:asyncio.Queue[Tuple[Client,Tuple,Dict]] = asyncio.Queue()

    async def run(self):
        while True:
            client,args,kwargs = await self._sending_queue.get()
            try:
                await client.send_async(*args,**kwargs)
            except ConnectionClosed:
                self._cleanup_client(client)

    def send(self,client:Client,*args,**kwargs):
        self._sending_queue.put_nowait((client,args,kwargs))

    async def handle_client(self,ws:WebSocketServerProtocol,path):
        '''
        Handle a client connection. 
        '''
        try:
            client_id = next(self._client_id_count)
            client = self._clients[client_id] = Client(client_id,ws,self._logger,self._sending_queue)
            self._logger.info(f"Client {client_id} connected")
            await client.send_async("hello",id=client_id)

            async for message in ws:
                self._logger.debug(f"> {message}")
                message_type, args = parse_message(message)
                if message_type in self._message_handlers:
                    return_value = self._message_handlers[message_type](sender = client,**args)
                    if isinstance(return_value,Awaitable):
                        await return_value
                else:
                    self._logger.error(f"Unknown message type: {message_type}")
                    pass

        except ConnectionClosed as e:
            self._logger.info(f"Client {client_id} disconnected: {repr(e)}")
            self._cleanup_client(client)
        except Exception as e:
            self._logger.error(f"Error handling client {client_id}:\n{traceback.format_exc()}")
            self._cleanup_client(client)

    def send_update(self,changes:List[Change],action_id:str):
        '''
        Broadcast a list of changes to all clients subscribed to the topics in the changes.
        '''
        messages_for_client = defaultdict(list)
        for change in changes:
            for client_id in self._subscriptions[change.topic_name]:
                messages_for_client[client_id].append(change.serialize())

        for client_id in messages_for_client:
            client = self._clients[client_id]
            self.send(client,"update",changes=messages_for_client[client_id],action_id=action_id)
    
    def register_message_handler(self,message_type:str,handler:Callable[...,None|Awaitable[None]]):
        self._message_handlers[message_type] = handler

    def _cleanup_client(self,client:Client):
        for topic in self._subscriptions:
            self._subscriptions[topic].discard(client.id)

    def _handle_subscribe(self,sender:Client,topic_name:str):
        self._subscriptions[topic_name].add(sender.id)
        self._logger.info(f"Client {sender.id} subscribed to {topic_name}")
        value = self._get_topic_value(topic_name)
        self.send(sender,"update",changes=[SetChange(topic_name,value).serialize()],action_id="")

    def _handle_unsubscribe(self,sender:Client,topic_name:str):
        self._subscriptions[topic_name].discard(sender.id)
        