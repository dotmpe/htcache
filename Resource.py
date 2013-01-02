"""
Resource storage and descriptor facade.
"""
import anydbm, os, urlparse
from os.path import join

try:
    # Py >= 2.4
    assert set
except AssertionError:
    from sets import Set as set

from sqlalchemy import Column, Integer, String, Boolean, Text, \
    ForeignKey, Table, Index, DateTime, \
    create_engine
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, backref, sessionmaker

import Cache
import Params
import HTTP
import Rules
import Runtime
from util import *
from error import *
from pprint import pformat



class ProxyData(object):

    """
    A facade for instances in datastore seen from a Descriptor instance.

    Web Resources::

        { <res-uri> : <Resource(

           host, path, meta, cache

        )> }
    
    Map of broken loations that could not be retrieved::

        { <res-uri> : <status>, <keep-boolean> }
    
    Cache descriptors::

        { <cache-location> : <Descriptor(

            hash, mediatype, charset, language, size, quality

        ) }

    Map of uriref to cache locations (reverse for resources)::

        { <cache-location> : <res-uri> }

    Qualified relations 'rel' from 'res' to 'ref'::

        relations_to = { <res-uri> => *( <rel-uri>, <ref-uri> ) }
    
    Reverse mapping, the qualification will be in relations_to::

        relations_from = { <ref-uri> => *<res-uri> }
    """

    def __init__( self, protocol ):
        self.protocol = protocol
        self.descriptor = None
        self.cache = None

    def is_open( self ):
        return self.cache and self.cache.file != None

    def init_cache( self ):
        """
        Opens the cache location for the current URL.

        The location will be subject to the specific heuristics of the backend
        type, this path will be readable from cache.path.
        """
        protocol = self.protocol
        assert protocol.url[:2] == '//', protocol.url
        netpath = protocol.url[2:]
        # XXX: record rewrites in descriptor DB?
        log( "Init cache: %s" % ( Runtime.CACHE, ), Params.LOG_DEBUG )
        self.cache = Cache.load_backend_type( Runtime.CACHE )( netpath )
        self.partial, self.complete = self.cache.partial(), self.cache.full()
        log( 'Prepped cache, position: %s' % self.cache.path, Params.LOG_INFO )

    def open_cache( self ):
        assert self.cache.path
        if self.partial:
            self.cache.open_partial()
        elif self.complete:
            self.cache.open_full()
        else:
            self.cache.open_new()

    def init_data(self):
        """
        After a client sends request headers, 
        initialize check for existing data or initialize a new descriptor.
        If new, the associated resource is initalized later.
        """
        assert self.cache.path and self.cache.path[0] != os.sep,\
            "call init_cache, should be relative path"
        descriptor = Descriptor()
        self.descriptor = descriptor.find(
            Descriptor.path == self.cache.path
        )
        if not self.descriptor or self.descriptor.id:
            descriptor.path = self.cache.path
            self.descriptor = descriptor
        log('ProxyData.init_data %r '%( self.descriptor ),
                Params.LOG_DEBUG)
        
    def init_and_open(self):
        """
        Set cache location and open any partial or complete file, 
        then fetch descriptor if it exists. 
        """
        self.init_cache()
        self.init_data()
        assert self.descriptor.id and self.complete, \
                "Nothing there to open. "
        self.open_cache()
        #assert self.data.is_open() and self.cache.full(), \
        #    "XXX: sanity check, cannot have partial served content, serve error instead"

    def update_data(self):
# after server response headers
        data = HTTP.map_headers_to_descriptor(
                self.protocol.headers )
        for k in data:
            if not hasattr( self.descriptor, k ):
                log("Unknown descriptor field from headers: %s" % k, Params.LOG_ERR)
                continue
            setattr( self.descriptor, k, data[k] )
        log('ProxyData.update_data %r %r '%( data, self.descriptor ),
                Params.LOG_DEBUG)

# before client response headers
    def finish_data(self):
        if not self.descriptor.id:
            res = Resource()
            res.url = self.protocol.url
            self.descriptor.resource = res
            res.commit()
            self.descriptor.commit()
        log('ProxyData.finish_data %r %r '%( self.descriptor, self.descriptor.resource ),
                Params.LOG_DEBUG)

    ## Proxy lifecycle hooks

    def prepare_request( self, request ):
        """
        Called by protocol to provide updated request headers.
        """
        log("ProxyData.prepare_request", Params.LOG_DEBUG)
        req_headers = request.headers

        self.init_cache()
        self.init_data()

        req_headers.pop( 'Accept-Encoding', None )
        htrange = req_headers.pop( 'Range', None )
        assert not htrange,\
                "XXX: Req for %s had a range: %s" % (self.protocol.url, htrange)

        # if expires < now: revalidate
        # TODO: RFC 2616 14.9.4: Cache revalidation and reload controls
        cache_control = req_headers.pop( 'Cache-Control', None )
        # HTTP/1.0 compat
        #if not cache_control:
        #    cache_control = req_headers.pop( 'Pragma', None )
        #    if cache_control:
        #        assert cache_control.strip() == "no-cache"
        #        req_headers['Cache-Control'] = "no-cache"

        if self.is_open():
            mdtime = time.strftime(
                Params.TIMEFMT, time.gmtime( self.cache.mtime ) )

            if self.partial:
                size = self.cache.size
                log('Requesting resume of partial file in cache: '
                    '%i bytes, %s' % ( size, mdtime ))
                req_headers[ 'Range' ] = 'bytes=%i-' % size
                req_headers[ 'If-Range' ] = mdtime

            elif self.complete:
                log('Checking complete file in cache: %i bytes, %s' %
                    ( size, mdtime ), 1)
                # XXX: treat as unspecified end-to-end revalidation
                # should detect existing cache-validating conditional?
                # TODO: Validate client validator against cached entry
                req_headers[ 'If-Modified-Since' ] = mdtime

                # XXX: don't gateway conditions, client seems to have cache but this is
                # a miss for the proxy
#            req_headers.pop( 'If-None-Match', None )
#            req_headers.pop( 'If-Modified-Since', None )

        # TODO: Store relationship with referer
        relationtype = req_headers.pop('X-Relationship', None)
        referer = req_headers.get('Referer', None)
        if referer:
            #self.relate(relationtype, self.url, referer)
            pass

        return req_headers

    def prepare_response( self ):
        log("ProxyData.prepare_response", Params.LOG_DEBUG)

        if not self.descriptor.id:
            # XXX: allow for opaque moves of descriptors
            if self.cache.path != self.descriptor.path:
                assert not ( self.partial or self.complete )
                self.cache.path = self.descriptor.path
                self.partial, self.complete = self.cache.partial(), self.cache.partial()
            # /XXX
            self.update_data()
        else:
            assert self.cache.path == self.descriptor.path

        self.open_cache()
        self.finish_data()

        return self.protocol.headers

    def move( self ):
        log("ProxyData.move", Params.LOG_DEBUG)

    def set_broken( self ):
        log("ProxyData.set_broken", Params.LOG_DEBUG)

    def close(self):
        log("ProxyData.close", Params.LOG_DEBUG)
        del self.cache
        del self.descriptor


### Descriptor Storage types:

SqlBase = declarative_base()


class SessionMixin(object):

    """
    """

    sessions = {}

    @staticmethod
    def get_instance(name='default', dbref=None, init=False, read_only=False):
        # XXX: read_only
        if name not in SessionMixin.sessions:
            assert dbref, "session does not exists: %s" % name
            session = get_session(dbref, init)
            #assert session.engine, "new session has no engine"
            SessionMixin.sessions[name] = session
        else:
            session = SessionMixin.sessions[name]
            #assert session.engine, "existing session does not have engine"
        return session

    @staticmethod
    def close_instance(name='default', dbref=None, init=False, read_only=False):
        if name in SessionMixin.sessions:
            session = SessionMixin.sessions[name]
        session.close()

    # XXX: SessionMixin.key_names
    key_names = []

#    def __nonzero__(self):
#        return self.id != None
#
    def key(self):
        key = {}
        for a in self.key_names:
            key[a] = getattr(self, a)
        return key

    def commit(self):
        session = SessionMixin.get_instance()
        session.add(self)
        session.commit()

    def find(self, qdict=None):
        try:
            return self.fetch(qdict=qdict)
        except NoResultFound, e:
            log("No results for %s.find(%s)" % (cn(self), qdict),
                    Params.LOG_INFO)

    def fetch(self, qdict=None):
        """
        Keydict must be filter parameters that return exactly one record.
        """
        session = SessionMixin.get_instance()
        if not qdict:
            qdict = self.key()
        return session.query(self.__class__).filter(**qdict).one()

    def exists(self):
        return self.fetch() != None 

    def __repr__(self):
        return self.__str__()


class Resource(SqlBase, SessionMixin):
    """
    """
    __tablename__ = 'resources'
    id = Column(Integer, primary_key=True)
    url = Column(String(255), nullable=False)
#    host = Column(String(255), nullable=False)
#    path = Column(String(255), nullable=False)
#    key_names = [id]
    
    def __str__(self):
        return "Resource(%s)" % pformat(dict(
            id=self.id,
            url=self.url,
        ))

class Descriptor(SqlBase, SessionMixin):
    """
    """
    __tablename__ = 'descriptors'

    id = Column(Integer, primary_key=True)
    resource_id = Column(Integer, ForeignKey(Resource.id), nullable=False)
    resource = relationship( Resource, 
#            primaryjoin=resource_id==Resource.id, 
            backref='descriptors')
    path = Column(String(255), nullable=True)
    mediatype = Column(String(255), nullable=False)
    charset = Column(String(255), nullable=True)
    language = Column(String(255), nullable=True)
    size = Column(Integer, nullable=True)
    quality = Column(Integer, nullable=True)
    etag = Column(String(255), nullable=True)
#    key_names = [id]
   
    def __str__(self):
        return "Descriptor(%s)" % pformat(dict(
            id=self.id,
            path=self.path,
            etag=self.etag,
            size=self.size,
            charset=self.charset,
            language=self.language,
            quality=self.quality,
            mediatype=self.mediatype
        ))

class Relation(SqlBase, SessionMixin):
    """
    """
    __tablename__ = 'relations'
    id = Column(Integer, primary_key=True)
    relate = Column(String(16), nullable=False)
    revuri = Column(Integer, ForeignKey(Resource.id), nullable=False)
    reluri = Column(Integer, ForeignKey(Resource.id), nullable=False)
#    key_names = [id]

    def __str__(self):
        return "Relation(%s)" % pformat(dict(
            id=self.id,
            relate=self.relate,
            revuri=self.revuri,
            reluri=self.reluri
        ))



#/FIXME

backend = None

def get_backend(read_only=False):
    global backend
    if not backend:
        backend = SessionMixin.get_instance(
                name='default', 
                dbref=Runtime.DATA, 
                init=True,
                read_only=read_only)
    return backend

def get_session(dbref, initialize=False):
    engine = create_engine(dbref)#, encoding='utf8')
    #engine.raw_connection().connection.text_factory = unicode
    if initialize:
        log("Applying SQL DDL to DB %s " % (dbref,), Params.LOG_DEBUG)
        SqlBase.metadata.create_all(engine)  # issue DDL create 
        log("Updated data schema", Params.LOG_INFO)
    session = sessionmaker(bind=engine)()
    return session



# Query commands 

def list_locations():
    global backend
    get_backend()

    for res in backend.query(Resource).all():
        print res
        for d in res.descriptors:
            print '\t', str(d).replace('\n', '\n\t')
    
    backend.close()

def list_urls():
    global backend
    get_backend()
    for url in backend.resources:
        res = backend.find(url)
        print res
        for d in res.descriptors:
            print '\t', str(d).replace('\n', '\n\t')

def print_record(url):
    get_backend()
    res = Resource().fetch(Resource.url == url)
    print res
    for d in res.descriptors:
        print '\t', str(d).replace('\n', '\n\t')

# TODO: find_records by attribute query
def find_records(q):
    import sys
    global backend
    get_backend()
    print 'Searching for', q

    attrpath, valuepattern = q.split(':')

    for path in backend:
        res = backend[path]
        urls, mime, qs, n, meta, feats = res
        for u in urls:
            if q in u:
                print path, mime, urls
#        for k in props:
#            if k in ('0','srcref'):
#                if props[k] in res[0]:
#                    print path
#            elif k in ('1','mediatype'):
#                if props[k] == res[1]:
#                    print path
#            elif k in ('2','charset'):
#                if props[k] == res[2]:
#                    print path
#            elif k in ('3','language'):
#                if props[k] in res[3]:
#                    print path
#            elif k in ('4','feature'):
#                for k2 in props[k]:
#                    if k2 not in res[4]:
#                        continue
#                    if res[4][k2] == props[k][k2]:
#                        print path
    backend.close()
    log("End of findinfo", Params.LOG_DEBUG)

# TODO: integrate with other print_info
def print_info(*paths):
    global backend
    open_backend(True)
    import sys
    recordcnt = 0
    for path in paths:
        if not path.startswith(os.sep):
            path = Params.ROOT + path
#        path = path.replace(Params.ROOT, '')
        if path not in backend:
            log("Unknown cache location: %s" % path, Params.LOG_CRIT)
        else:
            print path, backend.find(path)
            recordcnt += 1
    if recordcnt > 1:
        print >>sys.stderr, "Found %i records for %i paths" % (recordcnt,len(paths))
    elif recordcnt == 1:
        print >>sys.stderr, "Found one record"
    else:
        print >>sys.stderr, "No record found"
    backend.close()
    log("End of printinfo", Params.LOG_DEBUG)

def print_media_list(*media):
    "document, application, image, audio or video (or combination)"
    for m in media:
        # TODO: documents
        if m == 'image':
            for path in backend:
                res = backend[path]
                if 'image' in res[1]:
                    print path
        if m == 'audio':
            for path in backend:
                res = backend[path]
                if 'audio' in res[1]:
                    print path
        if m == 'videos':
            for path in backend:
                res = backend[path]
                if 'video' in res[1]:
                    print path

def check_data(cache, uripathnames, mediatype, d1, d2, meta, features):
    """
    References in descriptor cache must exist as file.
    This checks existence and the size property,  if complete.

    All rules should be applied.
    """
    if not Params.VERBOSE:
        Params.VERBOSE = 1
    pathname = cache.path
    if cache.partial():
        pathname += '.incomplete'
    if not (cache.partial() or cache.full()):
        log("Missing %s" % pathname)
        return
    if 'Content-Length' not in meta:
        log("Missing content length of %s" % pathname)
        return
    length = int(meta['Content-Length'])
    if cache.full() and os.path.getsize(pathname) != length:
        log("Corrupt file: %s, size should be %s" % (pathname, length))
        return
    return True

def validate_cache(pathname, uripathnames, mediatype, d1, d2, meta, features):
    """
    Descriptor properties must match those of file.
    This recalculates the files checksum.
    """
    return True

def check_tree(pathname, uripathnames, mediatype, d1, d2, meta, features):
    return True

def check_files():
    backend = SessionMixin.get_instance(True)
# XXX old
    #if Params.PRUNE:
    #    descriptors = SessionMixin.get_instance()
    #else:
    #    descriptors = SessionMixin.get_instance(main=False)
    pcount, rcount = 0, 0
    log("Iterating paths in cache root location. ")

    for root, dirs, files in os.walk(Params.ROOT):

        # Ignore files in root
        if not root[len(Params.ROOT):]:
            continue

#        rdir = os.path.join(Params.ROOT, root)
        for f in dirs + files:
            f = os.path.join(root, f)
            #if path_ignore(f):
            #    continue
            pcount += 1
            if f not in backend.descriptors:
                if os.path.isfile(f):
                    log("Missing descriptor for %s" % f)
                    if Runtime.PRUNE:
                        size = os.path.getsize(f)
                        if size < Runtime.MAX_SIZE_PRUNE:
                            os.unlink(f)
                            log("Removed unknown file %s" % f)
                        else:
                            log("Keeping %sMB" % (size / (1024 ** 2)))#, f))
                elif not (os.path.isdir(f) or os.path.islink(f)):
                    log("Unrecognized path %s" % f)
            elif f in backend.descriptors:
                rcount += 1
                descr = backend.descriptors[f]
                assert isinstance(descr, Record)
                uriref = descr[0][0]
                log("Found resource %s" % uriref, threshold=1)
# XXX: hardcoded paths.. replace once Cache/Resource is properly implemented
                port = 80
                if len(descr[0]) != 1:
                    log("Multiple references %s" % f)
                    continue
                urlparts = urlparse.urlparse(uriref)
                hostname = urlparts.netloc
                pathname = urlparts.path[1:]
# XXX: cannot reconstruct--, or should always normalize?
                if urlparts.query:
                    #print urlparts
                    pathname += '?'+urlparts.query
                hostinfo = hostname, port
                cache = get_cache(hostinfo, pathname)
                #print 'got cache', cache.getsize(), cache.path
# end
    log("Finished checking %s cache locations, found %s resources" % (
        pcount, rcount))
    backend.close()

def check_cache():
    #term = Resource.TerminalController()
    #print term.render('${YELLOW}Warning:${NORMAL}'), 'paper is crinkled'
    #pb = Resource.ProgressBar(term, 'Iterating descriptors')
#    if Params.PRUNE:
#        descriptors = SessionMixin.get_instance()
#    else:
#        descriptors = SessionMixin.get_instance(main=False)
    backend = SessionMixin.get_instance(True)

    refs = backend.descriptors.keys()
    count = len(refs)
    log("Iterating %s descriptors" % count)
    for i, ref in enumerate(refs):
        log("%i, %s" % (i, ref), Params.LOG_DEBUG)
        descr = backend.descriptors[ref]
        log("Record data: [%s] %r" %(ref, descr.data,), 2)
        urirefs, mediatype, d1, d2, meta, features = descr
        #progress = float(i)/count
        #pb.update(progress, ref)
# XXX: hardcoded paths.. replace once Cache/Resource is properly implemented
        port = 80
        if len(urirefs) != 1:
            log("Multiple references %s" % ref)
            continue
        urlparts = urlparse.urlparse(urirefs[0])
        hostname = urlparts.netloc
        pathname = urlparts.path[1:]
# XXX: cannot reconstruct--, or should always normalize?
        if urlparts.query:
            #print urlparts
            pathname += '?'+urlparts.query
        hostinfo = hostname, port
        cache = get_cache(hostinfo, pathname)
# end
        act = None
        if not check_data(cache, *descr):
            if not Params.PRUNE:
                continue
            act = True
            if cache.full() or cache.partial():
                path = cache.path
                if cache.partial():
                    path += '.incomplete'
                if os.path.getsize(path) > Params.MAX_SIZE_PRUNE:
                    if Params.INTERACTIVE:
                        pass
                    log("Keeping %s" % path)
                    continue
                if os.path.isfile(path):
                    print 'size=', cache.getsize() / 1024**2
                    os.unlink(path)
                    log("Deleted %s" % path)
                else:
                    log("Unable to remove dir %s" % path)
            del backend.descriptors[ref]
            log("Removed %s" % cache.path)
    log("Finished checking %s cache descriptors" % count)
    backend.close()
    #pb.clear()


