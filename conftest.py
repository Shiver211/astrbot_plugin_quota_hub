"""让测试可以 ``from core.xxx import ...``（把插件根目录加入 sys.path）。"""

import logging
import os
import sys
import types

sys.path.insert(0, os.path.dirname(__file__))

if "astrbot" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot")

    # event
    event = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        def __init__(
            self, group_id="", sender_id="", sender_name="", text="", origin=""
        ):
            self.group_id = group_id
            self.sender_id = sender_id
            self.sender_name = sender_name
            self.text = text
            self.unified_msg_origin = origin
            self.extras = {}
            self.stopped = False
            self.sent_messages = []

        def get_group_id(self):
            return self.group_id

        def get_sender_id(self):
            return self.sender_id

        def get_sender_name(self):
            return self.sender_name

        def get_message_str(self):
            return self.text

        def set_extra(self, k, v):
            self.extras[k] = v

        def get_extra(self, k, default=None):
            return self.extras.get(k, default)

        async def send(self, msg):
            self.sent_messages.append(msg)

        def stop_event(self):
            self.stopped = True

        def is_admin(self):
            return False

        def plain_result(self, text):
            return text

    event.AstrMessageEvent = AstrMessageEvent

    # filter
    filter_mod = types.ModuleType("astrbot.api.event.filter")

    def deco(*args, **kwargs):
        def wrapper(fn):
            fn._deco_args = args
            fn._deco_kwargs = kwargs
            return fn

        return wrapper

    class _CommandRoot:
        def command(self, *a, **kw):
            return deco(*a, **kw)

    filter_mod.on_llm_request = deco
    filter_mod.on_llm_response = deco
    filter_mod.on_waiting_llm_request = deco
    filter_mod.command_group = lambda *a, **kw: lambda fn: _CommandRoot()
    filter_mod.command = deco
    filter_mod.permission_type = deco
    filter_mod.PermissionType = types.SimpleNamespace(ADMIN="ADMIN")
    event.filter = filter_mod

    # message
    core_mod = types.ModuleType("astrbot.core")
    core_msg = types.ModuleType("astrbot.core.message")
    core_res = types.ModuleType("astrbot.core.message.message_event_result")

    class MessageChain:
        def __init__(self):
            self.parts = []

        def message(self, t):
            self.parts.append(t)
            return self

    core_res.MessageChain = MessageChain
    core_res.MessageEventResult = object
    core_msg.message_event_result = core_res
    core_mod.message = core_msg

    # star
    star_mod = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context=None):
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir(name):
            p = os.path.join(os.path.dirname(__file__), "data_test")
            os.makedirs(p, exist_ok=True)
            return p

    star_mod.Star = Star
    star_mod.StarTools = StarTools
    star_mod.Context = object

    api.event = event
    api.star = star_mod
    astrbot.api = api
    astrbot.core = core_mod
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.event.filter"] = filter_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.core"] = core_mod
    sys.modules["astrbot.core.message"] = core_msg
    sys.modules["astrbot.core.message.message_event_result"] = core_res


