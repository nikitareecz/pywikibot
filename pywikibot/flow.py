# -*- coding: utf-8 -*-
"""Objects representing Flow entities, like boards, topics, and posts."""
#
# (C) Pywikibot team, 2015-2020
#
# Distributed under the terms of the MIT license.
#
import abc
import logging

from urllib.parse import urlparse, parse_qs

from pywikibot.exceptions import NoPage, UnknownExtension, LockedPage
from pywikibot.page import BasePage, User
from pywikibot.tools import deprecate_arg


logger = logging.getLogger('pywiki.wiki.flow')


# Flow page-like objects (boards and topics)
class FlowPage(BasePage, abc.ABC):

    """The base page meta class for the Flow extension.

    It cannot be instantiated directly.
    """

    def __init__(self, source, title: str = ''):
        """Initializer.

        @param source: A Flow-enabled site or a Link or Page on such a site
        @type source: Site, pywikibot.page.Link, or pywikibot.page.Page
        @param title: normalized title of the page

        @raises TypeError: incorrect use of parameters
        @raises ValueError: use of non-Flow-enabled Site
        """
        super().__init__(source, title)

        if not self.site.has_extension('Flow'):
            raise UnknownExtension('site is not Flow-enabled')

    @abc.abstractmethod
    def _load(self, force: bool = False):
        """Abstract method to load and cache the Flow data.

        Subclasses must overwrite _load() method to load and cache
        the object's internal data from the API.
        """
        raise NotImplementedError

    @property
    def uuid(self) -> str:
        """Return the UUID of the page.

        @return: UUID of the page
        """
        if not hasattr(self, '_uuid'):
            self._uuid = self._load()['workflowId']
        return self._uuid

    def get(self, force=False, get_redirect=False):
        """Get the page's content."""
        if get_redirect or force:
            raise NotImplementedError(
                "Neither 'force' nor 'get_redirect' parameter is implemented "
                'in {}.get()'.format(self.__class__.__name__))

        # TODO: Return more useful data
        return self._data


class Board(FlowPage):

    """A Flow discussion board."""

    def _load(self, force: bool = False):
        """Load and cache the Board's data, derived from its topic list.

        @param force: Whether to force a reload if the data is already loaded
        """
        if not hasattr(self, '_data') or force:
            self._data = self.site.load_board(self)
        return self._data

    def _parse_url(self, links):
        """Parse a URL retrieved from the API."""
        rule = links['fwd']
        parsed_url = urlparse(rule['url'])
        params = parse_qs(parsed_url.query)
        new_params = {}
        for key, value in params.items():
            if key != 'title':
                key = key.replace('topiclist_', '').replace('-', '_')
                if key == 'offset_dir':
                    new_params['reverse'] = (value == 'rev')
                else:
                    new_params[key] = value
        return new_params

    @deprecate_arg('format', 'content_format')
    def topics(self, content_format: str = 'wikitext', limit: int = 100,
               sort_by: str = 'newest', offset=None, offset_uuid: str = '',
               reverse: bool = False, include_offset: bool = False,
               toc_only: bool = False):
        """Load this board's topics.

        @param content_format: The content format to request the data in;
            must be either 'wikitext', 'html', or 'fixed-html'
        @param limit: The number of topics to fetch in each request.
        @param sort_by: Algorithm to sort topics by;
            must be either 'newest' or 'updated'
        @param offset: The timestamp to start at (when sortby is 'updated').
        @type offset: Timestamp or equivalent str
        @param offset_uuid: The UUID to start at (when sortby is 'newest').
        @param reverse: Whether to reverse the topic ordering.
        @param include_offset: Whether to include the offset topic.
        @param toc_only: Whether to only include information for the TOC.
        @return: A generator of this board's topics.
        @rtype: generator of Topic objects
        """
        data = self.site.load_topiclist(self, content_format=content_format,
                                        limit=limit, sortby=sort_by,
                                        toconly=toc_only, offset=offset,
                                        offset_id=offset_uuid, reverse=reverse,
                                        include_offset=include_offset)
        while data['roots']:
            for root in data['roots']:
                topic = Topic.from_topiclist_data(self, root, data)
                yield topic
            cont_args = self._parse_url(data['links']['pagination'])
            data = self.site.load_topiclist(self, **cont_args)

    @deprecate_arg('format', 'content_format')
    def new_topic(self, title: str, content: str,
                  content_format: str = 'wikitext'):
        """Create and return a Topic object for a new topic on this Board.

        @param title: The title of the new topic (must be in plaintext)
        @param content: The content of the topic's initial post
        @param content_format: The content format of the supplied content;
            either 'wikitext' or 'html'
        @return: The new topic
        @rtype: Topic
        """
        return Topic.create_topic(self, title, content, content_format)


class Topic(FlowPage):

    """A Flow discussion topic."""

    def _load(self, force: bool = False, content_format: str = 'wikitext'):
        """Load and cache the Topic's data.

        @param force: Whether to force a reload if the data is already loaded
        @param content_format: The post format in which to load
        """
        if not hasattr(self, '_data') or force:
            self._data = self.site.load_topic(self, content_format)
        return self._data

    def _reload(self):
        """Forcibly reload the topic's root post."""
        self.root._load(load_from_topic=True)

    @classmethod
    @deprecate_arg('format', 'content_format')
    def create_topic(cls, board, title: str, content: str,
                     content_format: str = 'wikitext'):
        """Create and return a Topic object for a new topic on a Board.

        @param board: The topic's parent board
        @type board: Board
        @param title: The title of the new topic (must be in plaintext)
        @param content: The content of the topic's initial post
        @param content_format: The content format of the supplied content;
            either 'wikitext' or 'html'
        @return: The new topic
        @rtype: Topic
        """
        data = board.site.create_new_topic(board, title, content,
                                           content_format)
        return cls(board.site, data['topic-page'])

    @classmethod
    def from_topiclist_data(cls, board, root_uuid: str, topiclist_data: dict):
        """Create a Topic object from API data.

        @param board: The topic's parent Flow board
        @type board: Board
        @param root_uuid: The UUID of the topic and its root post
        @param topiclist_data: The data returned by view-topiclist
        @return: A Topic object derived from the supplied data
        @rtype: Topic
        @raises TypeError: any passed parameters have wrong types
        @raises ValueError: the passed topiclist_data is missing required data
        """
        if not isinstance(board, Board):
            raise TypeError('board must be a pywikibot.flow.Board object.')
        if not isinstance(root_uuid, str):
            raise TypeError('Topic/root UUID must be a string.')

        topic = cls(board.site, 'Topic:' + root_uuid)
        topic._root = Post.fromJSON(topic, root_uuid, topiclist_data)
        topic._uuid = root_uuid
        return topic

    @property
    def root(self):
        """The root post of this topic."""
        if not hasattr(self, '_root'):
            self._root = Post.fromJSON(self, self.uuid, self._data)
        return self._root

    @property
    def is_locked(self):
        """Whether this topic is locked."""
        return self.root._current_revision['isLocked']

    @property
    def is_moderated(self):
        """Whether this topic is moderated."""
        return self.root._current_revision['isModerated']

    @deprecate_arg('format', 'content_format')
    def replies(self, content_format: str = 'wikitext', force: bool = False):
        """A list of replies to this topic's root post.

        @param content_format: Content format to return contents in;
            must be 'wikitext', 'html', or 'fixed-html'
        @param force: Whether to reload from the API instead of using the cache
        @return: The replies of this topic's root post
        @rtype: list of Posts
        """
        return self.root.replies(content_format=content_format, force=force)

    @deprecate_arg('format', 'content_format')
    def reply(self, content: str, content_format: str = 'wikitext'):
        """A convenience method to reply to this topic's root post.

        @param content: The content of the new post
        @param content_format: The format of the given content;
            must be 'wikitext' or 'html')
        @return: The new reply to this topic's root post
        @rtype: Post
        """
        return self.root.reply(content, content_format)

    # Moderation
    def lock(self, reason: str):
        """Lock this topic.

        @param reason: The reason for locking this topic
        """
        self.site.lock_topic(self, True, reason)
        self._reload()

    def unlock(self, reason: str):
        """Unlock this topic.

        @param reason: The reason for unlocking this topic
        """
        self.site.lock_topic(self, False, reason)
        self._reload()

    def delete_mod(self, reason: str):
        """Delete this topic through the Flow moderation system.

        @param reason: The reason for deleting this topic.
        """
        self.site.delete_topic(self, reason)
        self._reload()

    def hide(self, reason: str):
        """Hide this topic.

        @param reason: The reason for hiding this topic.
        """
        self.site.hide_topic(self, reason)
        self._reload()

    def suppress(self, reason: str):
        """Suppress this topic.

        @param reason: The reason for suppressing this topic.
        """
        self.site.suppress_topic(self, reason)
        self._reload()

    def restore(self, reason: str):
        """Restore this topic.

        @param reason: The reason for restoring this topic.
        """
        self.site.restore_topic(self, reason)
        self._reload()


# Flow non-page-like objects
class Post:

    """A post to a Flow discussion topic."""

    def __init__(self, page, uuid: str):
        """
        Initializer.

        @param page: Flow topic
        @type page: Topic
        @param uuid: UUID of a Flow post

        @raises TypeError: incorrect types of parameters
        """
        if not isinstance(page, Topic):
            raise TypeError('Page must be a Topic object')
        if not page.exists():
            raise NoPage(page, 'Topic must exist: %s')
        if not isinstance(uuid, str):
            raise TypeError('Post UUID must be a string')

        self._page = page
        self._uuid = uuid

        self._content = {}

    @classmethod
    def fromJSON(cls, page, post_uuid: str, data: dict):
        """
        Create a Post object using the data returned from the API call.

        @param page: A Flow topic
        @type page: Topic
        @param post_uuid: The UUID of the post
        @param data: The JSON data returned from the API

        @return: A Post object
        @raises TypeError: data is not a dict
        @raises ValueError: data is missing required entries
        """
        post = cls(page, post_uuid)
        post._set_data(data)

        return post

    def _set_data(self, data: dict):
        """Set internal data and cache content.

        @param data: The data to store internally
        @raises TypeError: data is not a dict
        @raises ValueError: missing data entries or post/revision not found
        """
        if not isinstance(data, dict):
            raise TypeError('Illegal post data (must be a dictionary).')
        if ('posts' not in data) or ('revisions' not in data):
            raise ValueError('Illegal post data (missing required data).')
        if self.uuid not in data['posts']:
            raise ValueError('Post not found in supplied data.')

        current_revision_id = data['posts'][self.uuid][0]
        if current_revision_id not in data['revisions']:
            raise ValueError('Current revision of post'
                             'not found in supplied data.')

        self._current_revision = data['revisions'][current_revision_id]
        if 'content' in self._current_revision:
            content = self._current_revision.pop('content')
            assert isinstance(content, dict)
            assert isinstance(content['content'], str)
            self._content[content['format']] = content['content']

    def _load(self, force: bool = True, content_format: str = 'wikitext',
              load_from_topic: bool = False):
        """Load and cache the Post's data using the given content format.

        @param load_from_topic: Whether to load the post from the whole topic
        """
        if load_from_topic:
            data = self.page._load(force=force, content_format=content_format)
        else:
            data = self.site.load_post_current_revision(self.page, self.uuid,
                                                        content_format)
        self._set_data(data)
        return self._current_revision

    @property
    def uuid(self) -> str:
        """Return the UUID of the post.

        @return: UUID of the post
        """
        return self._uuid

    @property
    def site(self):
        """Return the site associated with the post.

        @return: Site associated with the post
        @rtype: Site
        """
        return self._page.site

    @property
    def page(self):
        """Return the page associated with the post.

        @return: Page associated with the post
        @rtype: Topic
        """
        return self._page

    @property
    def is_moderated(self):
        """Whether this post is moderated."""
        if not hasattr(self, '_current_revision'):
            self._load()
        return self._current_revision['isModerated']

    @property
    def creator(self):
        """The creator of this post."""
        if not hasattr(self, '_current_revision'):
            self._load()
        if not hasattr(self, '_creator'):
            self._creator = User(self.site,
                                 self._current_revision['creator']['name'])
        return self._creator

    @deprecate_arg('format', 'content_format')
    def get(self, content_format: str = 'wikitext',
            force: bool = False) -> str:
        """Return the contents of the post in the given format.

        @param force: Whether to reload from the API instead of using the cache
        @param content_format: Content format to return contents in
        @return: The contents of the post in the given content format
        """
        if content_format not in self._content or force:
            self._load(content_format=content_format)
        return self._content[content_format]

    @deprecate_arg('format', 'content_format')
    def replies(self, content_format: str = 'wikitext', force: bool = False):
        """Return this post's replies.

        @param content_format: Content format to return contents in;
            must be 'wikitext', 'html', or 'fixed-html'
        @param force: Whether to reload from the API instead of using the cache
        @return: This post's replies
        @rtype: list of Posts
        """
        if content_format not in ('wikitext', 'html', 'fixed-html'):
            raise ValueError('Invalid content format.')

        if hasattr(self, '_replies') and not force:
            return self._replies

        # load_from_topic workaround due to T106733
        # (replies not returned by view-post)
        if not hasattr(self, '_current_revision') or force:
            self._load(content_format=content_format, load_from_topic=True)

        reply_uuids = self._current_revision['replies']
        self._replies = [Post(self.page, uuid) for uuid in reply_uuids]

        return self._replies

    @deprecate_arg('format', 'content_format')
    def reply(self, content: str, content_format: str = 'wikitext'):
        """Reply to this post.

        @param content: The content of the new post
        @param content_format: The format of the given content;
            must be 'wikitext' or 'html'
        @return: The new reply post
        @rtype: Post
        """
        self._load()
        if self.page.is_locked:
            raise LockedPage(self.page, 'Topic %s is locked.')

        reply_url = self._current_revision['actions']['reply']['url']
        parsed_url = urlparse(reply_url)
        params = parse_qs(parsed_url.query)
        reply_to = params['topic_postId']
        if self.uuid == reply_to:
            del self._current_revision
            del self._replies
        data = self.site.reply_to_post(self.page, reply_to, content,
                                       content_format)
        post = Post(self.page, data['post-id'])
        return post

    # Moderation
    def delete(self, reason: str):
        """Delete this post through the Flow moderation system.

        @param reason: The reason for deleting this post.
        """
        self.site.delete_post(self, reason)
        self._load()

    def hide(self, reason: str):
        """Hide this post.

        @param reason: The reason for hiding this post.
        """
        self.site.hide_post(self, reason)
        self._load()

    def suppress(self, reason: str):
        """Suppress this post.

        @param reason: The reason for suppressing this post.
        """
        self.site.suppress_post(self, reason)
        self._load()

    def restore(self, reason: str):
        """Restore this post.

        @param reason: The reason for restoring this post.
        """
        self.site.restore_post(self, reason)
        self._load()

    def thank(self):
        """Thank the user who made this post."""
        self.site.thank_post(self)
