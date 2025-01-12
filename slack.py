# localslackirc
# Copyright (C) 2018 Salvo "LtWorf" Tomaselli
#
# localslackirc is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# author Salvo "LtWorf" Tomaselli <tiposchi@tiscali.it>

import datetime
from functools import lru_cache
from time import sleep, time
from typing import *

from slackclient import SlackClient
from typedload import load, dump

from diff import seddiff


USELESS_EVENTS = {
    'channel_marked',
    'group_marked',
    'hello',
    'dnd_updated_user',
    'reaction_added',
    'user_typing',
    'file_deleted',
    'file_public',
}


def _loadwrapper(value, type_):
    try:
        return load(value, type_)
    except Exception as e:
        print(e)
        pass


class ResponseException(Exception):
    pass


class Response(NamedTuple):
    """
    Internally used to parse a response from the API.
    """
    ok: bool
    headers: Dict[str, str]
    ts: Optional[float] = None


class Topic(NamedTuple):
    """
    In slack, topic is not just a string, but has other fields.
    """
    value: str


class LatestMessage(NamedTuple):
    ts: float

    @property
    def timestamp(self):
        return datetime.datetime.utcfromtimestamp(self.ts)


class Channel(NamedTuple):
    """
    A channel description.

    real_topic tries to use the purpose if the topic is missing
    """
    id: str
    name_normalized: str
    purpose: Topic
    topic: Topic
    num_members: int = 0
    #: Membership: present on channels, not on groups - but True there.
    is_member: bool = True

    #: Object type. groups have is_group=True, channels is_channel=True
    is_channel: bool = False
    is_group: bool = False
    is_mpim: bool = False

    latest: Optional[LatestMessage] = None

    @property
    def name(self):
        return self.name_normalized

    @property
    def real_topic(self) -> str:
        if self.topic.value:
            t = self.topic.value
        else:
            t = self.purpose.value
        return t.replace('\n', ' | ')


class Message(NamedTuple):
    channel: str  # The channel id
    user: str  # The user id
    text: str


class ActionMessage(Message):
    pass


class MessageEdit(NamedTuple):
    previous: Message
    current: Message

    @property
    def is_changed(self) -> bool:
        return self.previous.text != self.current.text

    @property
    def diffmsg(self) -> Message:
        m = dump(self.current)
        m['text'] = seddiff(self.previous.text, self.current.text)
        return load(m, Message)


class MessageDelete(Message):
    pass


class Profile(NamedTuple):
    real_name: str = 'noname'
    email: Optional[str] = None
    status_text: str = ''
    is_restricted: bool = False
    is_ultra_restricted: bool = False


class File(NamedTuple):
    id: str
    url_private: str
    size: int
    user: str
    name: Optional[str] = None
    title: Optional[str] = None
    mimetype: Optional[str] = None
    channels: List[str] = list()
    groups: List[str] = list()
    ims: List[str] = list()

    def announce(self) -> Message:
        """
        Returns a message to announce this file.
        """
        return Message(
            channel=(self.channels + self.groups + self.ims).pop(),
            user=self.user,
            text='[file upload] %s\n%s %d bytes\n%s' % (
                self.name,
                self.mimetype,
                self.size,
                self.url_private
            )
        )


class FileShared(NamedTuple):
    file_id: str
    user_id: str
    ts: float


class MessageBot(NamedTuple):
    text: str
    username: str
    channel: str
    bot_id: Optional[str] = None


class User(NamedTuple):
    id: str
    name: str
    profile: Profile
    is_admin: bool = False
    deleted: bool = False

    @property
    def real_name(self) -> str:
        return self.profile.real_name


class IM(NamedTuple):
    id: str
    user: str


class Join(NamedTuple):
    user: str
    channel: str


class Leave(NamedTuple):
    user: str
    channel: str


class TopicChange(NamedTuple):
    topic: str
    channel: str
    user: str

SlackEvent = Union[
    TopicChange,
    MessageDelete,
    MessageEdit,
    Message,
    ActionMessage,
    MessageBot,
    FileShared,
    Join,
    Leave,
]


class Slack:
    def __init__(self, token: str) -> None:
        self.client = SlackClient(token)
        self._usercache = {}  # type: Dict[str, User]
        self._usermapcache = {}  # type: Dict[str, User]
        self._imcache = {}  # type: Dict[str, str]
        self._get_members_cache = {}  # type: Dict[str, Set[str]]
        self._get_members_cache_cursor = {}  # type: Dict[str, Optional[str]]
        self._internalevents = []  # type: List[SlackEvent]
        self._sent_by_self = set()  # type: Set[float]

    def away(self, is_away: bool) -> None:
        """
        Forces the aways status or lets slack decide
        """
        status = 'away' if is_away else 'auto'
        r = self.client.api_call('users.setPresence', presence=status)
        response = load(r, Response)
        if not response.ok:
            raise ResponseException(response)

    def topic(self, channel: Channel, topic: str) -> None:
        r = self.client.api_call('conversations.setTopic', channel=channel.id, topic=topic)
        response = load(r, Response)
        if not response.ok:
            raise ResponseException(response)

    def kick(self, channel: Channel, user: User) -> None:
        r = self.client.api_call('conversations.kick', channel=channel.id, user=user.id)
        response = load(r, Response)
        if not response.ok:
            raise ResponseException(response)

    def invite(self, channel: Channel, user: Union[User, List[User]]) -> None:
        if isinstance(user, User):
            ids = user.id
        else:
            if len(user) > 30:
                raise ValueError('No more than 30 users allowed')
            ids = ','.join(i.id for i in user)

        r = self.client.api_call('conversations.invite', channel=channel.id, users=ids)
        response = load(r, Response)
        if not response.ok:
            raise ResponseException(response)

    def get_members(self, id_: str) -> Set[str]:
        """
        Returns the list (as a set) of users in a channel.

        It performs caching. Every time the function is called, a new batch is
        requested, until all the users are cached, and then no new requests
        are performed, and the same data is returned.

        When events happen, the cache needs to be updated or cleared.
        """
        cached = self._get_members_cache.get(id_, set())
        cursor = self._get_members_cache_cursor.get(id_)
        if cursor == '':
            # The cursor is fully iterated
            return cached
        kwargs = {}
        if cursor:
            kwargs['cursor'] = cursor
        r = self.client.api_call('conversations.members', channel=id_, limit=5000, **kwargs)  # type: ignore
        response = load(r, Response)
        if not response.ok:
            raise ResponseException(response)

        newusers = load(r['members'], Set[str])

        # Generate all the Join events, if this is not the 1st iteration
        if id_ in self._get_members_cache:
            for i in newusers.difference(cached):
                self._internalevents.append(Join(user=i, channel=id_))

        self._get_members_cache[id_] = cached.union(newusers)
        self._get_members_cache_cursor[id_] = r.get('response_metadata', {}).get('next_cursor')
        return self._get_members_cache[id_]

    @lru_cache()
    def channels(self) -> List[Channel]:
        """
        Returns the list of slack channels
        """
        result = []  # type: List[Channel]
        r = self.client.api_call("conversations.list", exclude_archived=True,
                types='public_channel,private_channel,mpim', limit=1000)
        response = load(r, Response)
        if response.ok:
            return load(r['channels'], List[Channel])
        else:
            raise ResponseException(response)


    @lru_cache()
    def get_channel(self, id_: str) -> Channel:
        """
        Returns a channel object from a slack channel id

        raises KeyError if it doesn't exist.
        """
        for c in self.channels():
            if c.id == id_:
                return c
        raise KeyError()

    @lru_cache()
    def get_channel_by_name(self, name: str) -> Channel:
        """
        Returns a channel object from a slack channel id

        raises KeyError if it doesn't exist.
        """
        for c in self.channels():
            if c.name == name:
                return c
        raise KeyError()

    @property
    def fileno(self) -> Optional[int]:
        return self.client.fileno

    def get_im(self, im_id: str) -> Optional[IM]:
        if not im_id.startswith('D'):
            return None
        for uid, imid in self._imcache.items():
            if im_id == imid:
                return IM(user=uid, id=imid)

        for im in self.get_ims():
            self._imcache[im.user] = im.id
            if im.id == im_id:
                return im;
        return None

    def get_ims(self) -> List[IM]:
        """
        Returns a list of the IMs

        Some bullshit slack invented because 1 to 1 conversations
        need to have an ID to send to, you can't send directly to
        a user.
        """
        r = self.client.api_call(
            "conversations.list",
            exclude_archived=True,
            types='im', limit=1000
        )
        response = load(r, Response)
        if response.ok:
            return load(r['channels'], List[IM])
        raise ResponseException(response)

    def get_user_by_name(self, name: str) -> User:
        return self._usermapcache[name]

    def get_usernames(self) -> List[str]:
        return list(self._usermapcache.keys())

    def prefetch_users(self) -> None:
        """
        Prefetch all team members for the slack team.
        """
        r = self.client.api_call("users.list")
        response = load(r, Response)
        if response.ok:
            for user in load(r['members'], List[User]):
                self._usercache[user.id] = user
                self._usermapcache[user.name] = user

    def get_user(self, id_: str) -> User:
        """
        Returns a user object from a slack user id

        raises KeyError if it does not exist
        """
        if id_ in self._usercache:
            return self._usercache[id_]

        r = self.client.api_call("users.info", user=id_)
        response = load(r, Response)
        if response.ok:
            u = load(r['user'], User)
            self._usercache[id_] = u
            self._usermapcache[u.name] = u
            return u
        else:
            raise KeyError(response)

    def get_file(self, f: Union[FileShared, str]) -> File:
        """
        Returns a file object
        """
        fileid = f if isinstance(f, str) else f.file_id
        r = self.client.api_call("files.info", file=fileid)
        response = load(r, Response)
        if response.ok:
            return load(r['file'], File)
        else:
            raise KeyError(response)

    def send_file(self, channel_id: str, filename: str) -> None:
        """
        Send a file to a channel or group or whatever
        """
        with open(filename, 'rb') as f:
            files = {'file': f}

            r = self.client.api_call(
                'files.upload',
                channels=channel_id,
                files=files,
            )
        response = load(r, Response)
        if response.ok:
            return
        raise ResponseException(response)

    def _triage_sent_by_self(self) -> None:
        """
        Clear all the old leftovers in
        _sent_by_self
        """
        r = []
        for i in self._sent_by_self:
            if time() - i >= 10:
                r.append(i)
        for i in r:
            self._sent_by_self.remove(i)

    def send_message(self, channel_id: str, msg: str, action: bool) -> None:
        """
        Send a message to a channel or group or whatever
        """
        if action:
            api = 'chat.meMessage'
        else:
            api = 'chat.postMessage'
        r = self.client.api_call(
            api,
            channel=channel_id,
            text=msg,
            as_user=True,
        )
        response = load(r, Response)
        if response.ok and response.ts:
            self._sent_by_self.add(response.ts)
            return
        raise ResponseException(response)

    def send_message_to_user(self, user_id: str, msg: str, action: bool):
        """
        Send a message to a user, pass the user id
        """

        # 1 to 1 chats are like channels, but use a dedicated API,
        # so to deliver a message to them, a channel id is required.
        # Those are called IM.

        if user_id in self._imcache:
            # channel id is cached
            channel_id = self._imcache[user_id]
        else:
            # Find the channel id
            found = False
            # Iterate over all the existing conversations
            for i in self.get_ims():
                if i.user == user_id:
                    channel_id = i.id
                    found = True
                    break
            # A conversation does not exist, create one
            if not found:
                r = self.client.api_call(
                    "im.open",
                    return_im=True,
                    user=user_id,
                )
                response = load(r, Response)
                if not response.ok:
                    raise ResponseException(response)
                channel_id = r['channel']['id']

            self._imcache[user_id] = channel_id

        self.send_message(channel_id, msg, action)

    def events_iter(self) -> Iterator[Optional[SlackEvent]]:
        """
        This yields an event or None. Don't call it without sleeps
        """
        sleeptime = 1

        while True:
            while self._internalevents:
                yield self._internalevents.pop()

            try:
                events = self.client.rtm_read()
            except:
                print('Connecting to slack...')
                try:
                    self.login_info = self.client.rtm_connect()
                    sleeptime = 1
                except:
                    print('Connection to slack failed')
                    sleep(sleeptime)
                    if sleeptime <= 120:  # max reconnection interval at 2 minutes
                        sleeptime *= 2
                    continue
                print('Connected to slack')
                continue

            for event in events:
                t = event.get('type')
                subt = event.get('subtype')

                ts = float(event.get('ts', 0))
                if ts in self._sent_by_self:
                    self._sent_by_self.remove(ts)
                    continue

                try:
                    if t == 'message' and (not subt or subt == 'me_message'):
                        msg = _loadwrapper(event, Message)

                        # In private chats, pretend that my own messages
                        # sent from another client actually come from
                        # the other user, and prepend them with "I say: "
                        im = self.get_im(msg.channel)
                        if im and im.user != msg.user:
                            msg = Message(user=im.user, text='I say: ' + msg.text, channel=im.id)
                        if subt == 'me_message':
                            yield ActionMessage(*msg)
                        else:
                            yield msg
                    elif t == 'message' and subt == 'slackbot_response':
                        yield _loadwrapper(event, Message)
                    elif t == 'message' and subt == 'message_changed':
                        event['message']['channel'] = event['channel']
                        event['previous_message']['channel'] = event['channel']
                        yield MessageEdit(
                            previous=load(event['previous_message'], Message),
                            current=load(event['message'], Message)
                        )
                    elif t == 'message' and subt == 'group_topic':
                        yield _loadwrapper(event, TopicChange)
                    elif t == 'message' and subt == 'message_deleted':
                        event['previous_message']['channel'] = event['channel']
                        ev = _loadwrapper(event['previous_message'], MessageDelete)
                        if ev.text:  # deleting files generates empty MessageDelete events
                            yield ev
                    elif t == 'message' and subt == 'bot_message':
                        yield _loadwrapper(event, MessageBot)
                    elif t == 'member_joined_channel':
                        j = _loadwrapper(event, Join)
                        self._get_members_cache[j.channel].add(j.user)
                        yield j
                    elif t == 'member_left_channel':
                        j = _loadwrapper(event, Leave)
                        self._get_members_cache[j.channel].remove(j.user)
                        yield j
                    elif t == 'user_change':
                        # Changes in the user, drop it from cache
                        u = load(event['user'], User)
                        if u.id in self._usercache:
                            del self._usercache[u.id]
                            #FIXME don't know if it is wise, maybe it gets lost forever del self._usermapcache[u.name]
                        #TODO make an event for this
                    elif t == 'file_shared':
                        try: # slack idiocy workaround, they send the event twice, one time without the ts field
                            yield _loadwrapper(event, FileShared)
                        except ValueError:
                            pass
                    elif t in USELESS_EVENTS:
                        continue
                    else:
                        print(event)
                except Exception as e:
                    print('Exception: %s' % e)
            self._triage_sent_by_self()
            yield None
