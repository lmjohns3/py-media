import arrow
import base64
import click
import contextlib
import enum
import functools
import hashlib
import json
import numpy as np
import os
import parsimonious.grammar
import PIL.Image
import re
import sqlalchemy
import sqlalchemy.ext.declarative

from sqlalchemy import Column, DateTime, Enum, Float, ForeignKey, Integer, String, Table
from sqlalchemy.orm.attributes import flag_modified

from . import metadata
from . import tools


class TextJson(sqlalchemy.types.TypeDecorator):
    impl = sqlalchemy.types.TEXT

    def process_bind_param(self, value, dialect):
        return None if value is None else json.dumps(value)

    def process_result_value(self, value, dialect):
        return None if value is None else json.loads(value)

JSON = sqlalchemy.types.JSON().with_variant(TextJson, 'sqlite')


Model = sqlalchemy.ext.declarative.declarative_base()


class Tag(Model):
    __tablename__ = 'tags'

    id = Column(Integer, primary_key=True)
    name = Column(String, index=True, nullable=False)

    def __repr__(self):
        return f'<Tag {self.name}>'

    # Regular expression matchers for different "groups" of tags. The order here
    # is used to sort the tags on an asset. Tags not matching any of these
    # groups are "user-defined" and will sort before or after these.
    GROUPS = (
        # Year.
        r'(19|20)\d\d',

        # Month.
        'january', 'february', 'march', 'april', 'may', 'june', 'july',
        'august', 'september', 'october', 'november', 'december',

        # Day of month.
        r'\d(st|nd|rd|th)', r'\d\d(st|nd|rd|th)',

        # Day of week.
        'sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday',

        # Time of day.
        r'\dam', r'\d\dam', r'\dpm', r'\d\dpm',

        # Camera.
        r'(e-m\d|iphone|pixel|powershot)\S+',

        # Aperture.
        r'f/\d', r'f/\d\d', r'f/\d\d\d',

        # Focal length.
        r'\dmm', r'\d\dmm', r'\d\d\dmm', r'\d\d\d\dmm',

        # Geolocation.
        r'country:\S+', r'state:\S+', r'city:\S+', r'place:\S+',
    )

    @staticmethod
    def get_or_create(sess, name):
        tag = sess.query(Tag).filter(Tag.name == name).first()
        if not tag:
            tag = Tag(name=name)
            sess.add(tag)
        return tag

    @property
    def group(self):
        for i, group in enumerate(Tag.GROUPS):
            if group == self.name or re.match(group, self.name):
                return i
        return -1

    @property
    def name_string(self):
        return click.style(self.name, fg='blue', bold=True)

    def to_dict(self):
        return dict(id=self.id, name=self.name, group=self.group)


asset_tags = Table('asset_tags',
                   Model.metadata,
                   Column('asset_id', ForeignKey('assets.id'), index=True),
                   Column('tag_id', ForeignKey('tags.id'), index=True))

similar = Table('similar',
                Model.metadata,
                Column('a_id', ForeignKey('assets.id'), index=True),
                Column('b_id', ForeignKey('assets.id')))


class Asset(Model):
    __tablename__ = 'assets'

    @enum.unique
    class Medium(enum.Enum):
        '''Enumeration of different supported media types.'''
        Audio = 1
        Photo = 2
        Video = 3

    id = Column(Integer, primary_key=True)

    medium = Column(Enum(Medium), index=True, nullable=False)
    stamp = Column(DateTime, index=True, nullable=False)

    path = Column(String, nullable=False)
    description = Column(String, nullable=False, default='')

    width = Column(Integer)
    height = Column(Integer)
    duration = Column(Float)
    fps = Column(Float)
    lat = Column(Float, index=True)
    lng = Column(Float, index=True)

    filters = Column(JSON, nullable=False, default=[])

    tags = sqlalchemy.orm.relationship('Tag',
                                       secondary=asset_tags,
                                       collection_class=set,
                                       backref='assets',
                                       lazy='joined')

    similar = sqlalchemy.orm.relationship('Asset',
                                          secondary=similar,
                                          primaryjoin=id == similar.c.a_id,
                                          secondaryjoin=id == similar.c.b_id)

    @property
    def shape(self):
        return self.width, self.height, self.duration

    @property
    def basename(self):
        '''The base filename for this asset.'''
        return os.path.basename(self.path)

    @property
    def path_hash(self):
        '''A string containing the hash of this asset's path.'''
        digest = hashlib.md5(self.path.encode('utf-8')).digest()
        return base64.b64encode(digest, b'-_').strip(b'=').decode('utf-8')

    @property
    def md5_hash(self):
        return [h for h in self.hashes if h.flavor == Hash.Flavor.MD5][0]

    @property
    def diff8_hashes(self):
        return sorted(h for h in self.hashes if h.flavor == Hash.Flavor.DIFF_8)

    @staticmethod
    def matching(sess, query, order=None, limit=None, offset=None):
        '''Find one or more media assets by parsing a query.

        Parameters
        ----------
        sess : SQLAlchemy
            Database session.
        query : str
            Get assets from the database matching these query clauses.
        order : str
            Order assets by this field.
        limit : int
            Limit the number of returned assets.
        offset : int
            Start at this position in the asset list.

        Returns
        -------
          A result set of :class:`Asset`s matching the query.
        '''
        rs = sess.query(Asset)
        if query.strip():
            rs = rs.filter(QueryParser(sess).parse(query))
        if order:
            rs = rs.order_by(parse_order(order))
        if limit:
            rs = rs.limit(limit)
        if offset:
            rs = rs.offset(offset)
        return rs

    def to_dict(self, exclude_tags=()):
        return dict(
            id=self.id,
            path=self.path,
            path_hash=self.path_hash,
            medium=self.medium.name.lower(),
            filters=self.filters,
            stamp=arrow.get(self.stamp).isoformat(),
            description=self.description,
            width=self.width,
            height=self.height,
            duration=self.duration,
            fps=self.fps,
            latlng=(self.lat, self.lng),
            hashes=[h.to_dict() for h in self.hashes],
            tags=[t.to_dict() for t in self.tags
                  if t.name not in exclude_tags],
        )

    def export(self, root, fmt=None, overwrite=False, **kwargs):
        '''Export a version of this media asset to another location.

        Additional keyword arguments are used to create a :class:`Format` if
        `fmt` is `None`.

        Parameters
        ----------
        root : str
            Save exported media under this root path.
        fmt : :class:`Format`, optional
            Export media with the given :class:`Format`.
        overwrite : bool, optional
            If an exported file already exists, this flag determines what to
            do. If `True` overwrite it; otherwise (the default), return.

        Returns
        -------
        The path to the exported file, or None if nothing was exported.
        '''
        hash = self.path_hash

        if fmt is None:
            fmt = metadata.Format(**kwargs)

        dirname = os.path.join(root, str(fmt), hash[:2])
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        ext = fmt.extension_for(self.medium)

        output = os.path.join(dirname, f'{hash}.{ext}')
        if os.path.exists(output) and not overwrite:
            return None

        tools.ffmpeg(self, fmt, output)

        return output

    def _maybe_hide_original(self, hide_original=False):
        '''Rename the original source for this asset.

        WARNING: If `hide_original` is True, the original file will be renamed
        with a hidden (dot) prefix. These hidden prefix files can be garbage
        collected by some external process (e.g., cron).

        Parameters
        ----------
        hide_original : bool
            If this is True, the item's source file will be renamed with an
            ".illuminatus-removed-" prefix.
        '''
        # if desired, hide the original file referenced by this asset.
        if hide_original:
            hidden = os.path.join(
                os.path.dirname(self.path),
                '.illuminatus-removed-' + os.path.basename(self.path))
            os.rename(target.path, hidden)

    def _init(self, sess):
        '''Initialize a newly created asset.'''
        if not os.path.isfile(self.path):
            return

        meta = metadata.Metadata(self.path)
        self.lat, self.lng = meta.latitude, meta.longitude
        self.width, self.height, self.duration = meta.width, meta.height, meta.duration
        for t in meta.tags:
            self.tags.add(Tag.get_or_create(sess, t))

        stamp = meta.stamp or arrow.get(os.path.getmtime(path))
        if stamp:
            self.stamp = stamp.datetime
            for t in metadata.tags_from_stamp(stamp):
                self.tags.add(Tag.get_or_create(sess, t))

        self.hashes.append(Hash.compute_md5sum(self.path))
        if self.medium == Asset.Medium.Photo:
            self.hashes.append(Hash.compute_photo_diff(self.path))
            self.hashes.append(Hash.compute_photo_histogram(self.path))
        if self.medium == Asset.Medium.Video:
            for o in range(0, int(self.duration), 30):
                self.hashes.append(Hash.compute_video_diff(self.path, o + 15))

    def update_stamp(self, when):
        '''Update the timestamp for this asset.

        Parameters
        ----------
        when : str
            A modifier for the stamp for this asset.
        '''
        for t in metadata.gen_datetime_tags(arrow.get(self.stamp)):
            self.tags.remove(t)

        try:
            self.stamp = arrow.get(when).datetime
        except arrow.parser.ParserError:
            fields = dict(y='years', m='months', d='days', h='hours')
            kwargs = {}
            for spec in re.findall(r'[-+]\d+[ymdh]', when):
                sign, shift, granularity = spec[0], spec[1:-1], spec[-1]
                kwargs[fields[granularity]] = (-1 if sign == '-' else 1) * int(shift)
            self.stamp = arrow.get(self.stamp).replace(**kwargs).datetime

        for t in metadata.gen_datetime_tags(arrow.get(self.stamp)):
            self.tags.add(t)

    def add_filter(self, filter):
        '''Add a filter to this asset.

        Parameters
        ----------
        filter : dict
            A dictionary containing filter arguments. The dictionary must have
            a "filter" key that names a valid media filter.
        '''
        if not isinstance(self.filters, list):
            self.filters = []
        self.filters.append(filter)
        flag_modified(self, 'filters')

    def remove_filter(self, filter, index=-1):
        '''Remove a filter if the index matches.

        Parameters
        ----------
        filter : str
            A string-valued filter name, which must match the filter at the
            given `index`.
        index : int
            An integer index of the filter to remove. This can be negative,
            which indexes from the end of the filter list.

        Raises
        ------
        IndexError
            If the given `index` exceeds the number of filters for this asset.
        KeyError
            If the filter at the specified `index` does not have the given
            `key`.
        '''
        if not isinstance(self.filters, list):
            self.filters = []
        if not self.filters:
            return
        while index < 0:
            index += len(self.filters)
        if index >= len(self.filters):
            raise IndexError(f'{self.path}: does not have {index} filters')
        actual_filter = self.filters[index]['filter']
        if actual_filter != filter:
            raise KeyError(f'{self.path}: filter {index} has key '
                           f'{actual_filter!r}, expected {filter!r}')
        self.filters.pop(index)
        flag_modified(self, 'filters')


class Proposal(Model):
    __tablename__ = 'proposals'

    @enum.unique
    class Result(enum.Enum):
        Proposed = 0
        Rejected = 1
        Accepted = 2

    id = Column(Integer, primary_key=True)
    asset_id = Column('asset_id', ForeignKey('assets.id'), index=True)
    tag_id = Column('tag_id', ForeignKey('tags.id'), index=True)
    score = Column(Float, index=True, nullable=False)
    result = Column(Enum(Result), index=True)
    source = Column(String)

    asset = sqlalchemy.orm.relationship(Asset, backref='proposals')
    tag = sqlalchemy.orm.relationship(Tag, backref='proposals')


class Hash(Model):
    __tablename__ = 'hashes'

    @enum.unique
    class Flavor(enum.Enum):
        '''Enumeration of different supported hash types.'''
        MD5 = 0
        DIFF_8 = 1
        DIFF_16 = 2
        HSL_HIST = 100
        RGB_HIST = 101

    id = Column(Integer, primary_key=True)
    asset_id = Column(ForeignKey('assets.id'), index=True)
    nibbles = Column(String, index=True, nullable=False)
    flavor = Column(Enum(Flavor), index=True, nullable=False)
    time = Column(Float)

    asset = sqlalchemy.orm.relationship(Asset, backref='hashes')

    def __str__(self):
        return ':'.join((click.style(self.flavor.name, fg='white'),
                         click.style(self.nibbles, fg='white', bold=True)))

    def __lt__(self, other):
        return self.nibbles < other.nibbles

    @classmethod
    def compute_md5sum(cls, path):
        '''Compute an MD5 sum based on the contents of a file.

        Parameters
        ----------
        path : str
            Path to a file on disk.

        Returns
        -------
        A Hash instance representing the MD5 sum of this file's contents.
        '''
        with open(path, 'rb') as handle:
            nibbles = hashlib.md5(handle.read()).hexdigest()
        return cls(nibbles=nibbles, flavor=Hash.Flavor.MD5)

    @classmethod
    def compute_photo_diff(cls, path, size=8):
        '''Compute a similarity hash for an image.

        Parameters
        ----------
        path : str
            Path to an image file on disk.
        size : int, optional
            Number of pixels, `s`, per side for the image. The hash will have
            `s * s` bits. Must correspond to one of the available DIFF_N hash
            flavors.

        Returns
        -------
        A Hash instance representing the diff hash.
        '''
        gray = PIL.Image.open(path).convert('L')
        pixels = np.asarray(gray.resize((size + 1, size), PIL.Image.ANTIALIAS))
        diff = (pixels[:, 1:] > pixels[:, :-1]).ravel()
        value = int(''.join('01'[b] for b in diff), 2)
        return cls(nibbles=('{:0%dx}' % (size * size / 4)).format(value),
                   flavor=Hash.Flavor[f'DIFF_{size}'])

    @classmethod
    def compute_photo_histogram(cls, path):
        #img = PIL.ImageCms.applyTransform(
        #    PIL.Image.open(path).convert('RGB'),
        #    PIL.ImageCms.buildTransformFromOpenProfiles(
        #        PIL.ImageCms.createProfile('sRGB'),
        #        PIL.ImageCms.createProfile('LAB'),
        #        'RGB', 'LAB'))

        def quantize(counts, bins):
            eps = 1e-6
            parts = np.array([sum(c) for c in np.split(np.array(counts), bins)])
            logp = np.log(eps + parts) - np.log(eps * len(parts) + sum(parts))
            lo, hi = np.percentile(logp, [1, 99])
            quantized = np.linspace(lo, hi, 16).searchsorted(np.clip(logp, lo, hi))
            return ''.join(f'{b:x}' for b in quantized)

        hist = np.asarray(PIL.Image.open(path).convert('HSV').histogram())
        return cls(nibbles=''.join(quantize(h, 8) for h in np.split(hist, 3)),
                   flavor=Hash.Flavor.HSL_HIST)

    @classmethod
    def compute_audio_diff(cls, path, size=8):
        raise NotImplementedError

    @classmethod
    def compute_video_diff(cls, path, time, size=8):
        return cls(nibbles='', flavor=Hash.Flavor.DIFF_8, time=time)

    def select_neighbors(self, sess, within=1):
        '''Get all neighboring hashes from the database.

        Parameters
        ----------
        sess : SQLAlchemy
            Database session.
        within : int, optional
            Select all existing hashes within this Hamming distance.

        Returns
        -------
        A query object over neighboring hashes from our hash.
        '''
        return sess.query(Hash).filter(
            Hash.nibbles.in_(_neighboring_hashes(self.nibbles, within)),
            Hash.flavor == self.flavor)

    def to_dict(self):
        return dict(
            nibbles=self.nibbles,
            flavor=self.flavor,
            time=self.time,
        )


# a map from each hex digit to the hex digits that differ in 1 bit.
_HEX_NEIGHBORS = ('1248', '0359', '306a', '217b',
                  '560c', '471d', '742e', '653f',
                  '9ac0', '8bd1', 'b8e2', 'a9f3',
                  'de84', 'cf95', 'fca6', 'edb7')

def _neighboring_hashes(start, distance=1):
    '''Pull all neighboring hashes within a given Hamming distance.

    Parameters
    ----------
    start : str
        Hexadecimal string representing the source hash value.
    distance : int, optional
        Identify all hashes within this Hamming distance from the start.

    Returns
    -------
    The set of hashes that are within the given distance from the original.
    Does not include the start.
    '''
    neighborhood, frontier = set(), {start}
    for _ in range(distance):
        frontier_ = set()
        for node in frontier - neighborhood:
            for i, c in enumerate(node):
                for d in _HEX_NEIGHBORS[int(c, 16)]:
                    frontier_.add(node[:i] + d + node[i+1:])
        neighborhood |= frontier
        frontier = frontier_
    return (neighborhood | frontier) - {start}


class QueryParser(parsimonious.NodeVisitor):
    '''Media can be queried using a special query syntax; we parse it here.

    See docstring about query syntax in cli.py.
    '''

    grammar = parsimonious.Grammar(r'''
    query    = union ( __ union )*
    union    = negation ( __ 'or' __ negation )*
    negation = ( 'not' __ )? set
    set      = group / stamp / path / medium / hash / text
    group    = '(' _ query _ ')'
    stamp    = ~'(before|during|after):[-\d]+'
    path     = ~'path:\S+?'
    medium   = ~'(photo|video|audio)'
    hash     = ~'hash:[a-z0-9]+'
    text     = ~'"?[^ ()"]+"?'
    _        = ~'\s*'
    __       = ~'\s+'
    ''')

    def __init__(self, db):
        self.db = db

    def generic_visit(self, node, children):
        return children or node.text

    def visit_query(self, node, children):
        intersection, rest = children
        for elem in rest:
            intersection &= elem[-1]
        return intersection

    def visit_union(self, node, children):
        union, rest = children
        for elem in rest:
            union |= elem[-1]
        return union

    def visit_negation(self, node, children):
        neg, [which] = children
        return ~which if neg else which

    def visit_stamp(self, node, children):
        comp, value = node.text.split(':', 1)
        value = arrow.get(value, ['YYYY', 'YYYY-MM', 'YYYY-MM-DD']).date
        column = Asset.stamp
        return (column < value if comp == 'before' else
                column > value if comp == 'after' else
                column.startswith(value))

    def visit_path(self, node, children):
        return Asset.path.contains(node.text[4:])

    def visit_hash(self, node, children):
        return Asset.hashes.any(Hash.nibbles.contains((node.text[5:])))

    def visit_medium(self, node, children):
        return Asset.medium == node.text.split(':', 1)[1].capitalize()

    def visit_text(self, node, children):
        s = node.text.strip('"')
        print(s)
        return (Asset.description.contains(s) |
                Asset.tags.any(Tag.name == s))


def parse_order(order):
    '''Parse an ordering string into a SQL alchemy ordering spec.'''
    if order.lower().startswith('rand'):
        return sqlalchemy.func.random()
    descending = False
    if order.endswith('-'):
        descending = True
        order = order[:-1]
    how = getattr(Asset, order)
    return how.desc() if descending else how


@sqlalchemy.event.listens_for(sqlalchemy.engine.Engine, 'connect')
def set_sqlite_pragma(dbapi_connection, connection_record):
    cur = dbapi_connection.cursor()
    cur.execute('PRAGMA encoding = "UTF-8"')
    cur.execute('PRAGMA foreign_keys = ON')
    cur.execute('PRAGMA journal_mode = WAL')
    cur.execute('PRAGMA synchronous = NORMAL')
    cur.close()


def engine(path, echo=False):
    return sqlalchemy.create_engine(f'sqlite:///{path}', echo=echo)


def init(path):
    Model.metadata.create_all(engine(path))


@contextlib.contextmanager
def session(path, echo=False, hide_original_on_delete=False):
    session = sqlalchemy.orm.scoping.scoped_session(
        sqlalchemy.orm.sessionmaker(bind=engine(path, echo)))

    @sqlalchemy.event.listens_for(session, 'before_flush')
    def handle_asset_bookkeeping(sess, ctx, instances):
        for asset in sess.new:
            if isinstance(asset, Asset):
                asset._init(sess)
        for asset in sess.deleted:
            if isinstance(asset, Asset):
                asset._maybe_hide_original(hide_original_on_delete)

    try:
        yield session
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.remove()
