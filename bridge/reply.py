# encoding:utf-8

from enum import Enum


# The stand-in a cancelled turn carries when it produced nothing at all.
#
# It is NOT an answer: the run was aborted mid-flight, so there is nothing to
# report. A cancelled turn that DID produce partial output keeps that output
# and never reaches this string (agent/protocol/agent_stream.py only falls
# back to it when the response is empty).
#
# It lives here because both the producer (agent_stream) and the consumers
# (agent/tools/agent_delegate) need to agree on what "nothing" looks like -
# a duplicated literal would let the two drift apart and turn "the teammate
# was cut off" into "the teammate answered".
CANCELLED_PLACEHOLDER = "_(Cancelled)_"


class ReplyType(Enum):
    TEXT = 1  # 文本
    VOICE = 2  # 音频文件
    IMAGE = 3  # 图片文件
    IMAGE_URL = 4  # 图片URL
    VIDEO_URL = 5  # 视频URL
    FILE = 6  # 文件
    CARD = 7  # 微信名片，仅支持ntchat
    INVITE_ROOM = 8  # 邀请好友进群
    INFO = 9
    ERROR = 10
    TEXT_ = 11  # 强制文本
    VIDEO = 12
    MINIAPP = 13  # 小程序

    def __str__(self):
        return self.name


class Reply:
    def __init__(self, type: ReplyType = None, content=None):
        self.type = type
        self.content = content

    def __str__(self):
        return "Reply(type={}, content={})".format(self.type, self.content)
