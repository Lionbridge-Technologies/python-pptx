# -*- coding: utf-8 -*-
#
# packaging.py
#
# Copyright (C) 2012, 2013 Steve Canny scanny@cisco.com
#
# This module is part of python-pptx and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

'''
Code that deals with reading and writing presentations to and from a .pptx file.

As a general principle, this module hides the complexity of the package
directory structure, file reading and writing, zip file manipulation, and
relationship items, needing only an understanding of the high-level
Presentation API to do its work.

REFACTOR: Consider generalizing such that specific classes for part types and
part type collections are no longer required, and can be delivered by factory
functions as parameter driven or perhaps table driven. If that were possible,
this module might be easily adapted to WordprocessingML and SpreadsheetML
without code specific to either of those package formats.

'''

import os
import zipfile

from lxml import etree
from StringIO import StringIO

import pptx.spec

from pptx            import util
from pptx.exceptions import CorruptedPackageError, DuplicateKeyError,\
                            NotXMLError, PackageNotFoundError
from pptx.spec       import qname

import logging
log = logging.getLogger('pptx.packaging')
log.setLevel(logging.DEBUG)
# log.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
log.addHandler(ch)


# ============================================================================
# API Classes
# ============================================================================

class Package(object):
    """
    Load or save an Open XML package.
    
    Hides packaging complexities.
    
    """
    def __init__(self):
        """
        Construct and initialize a new package instance. Package is initially
        empty, call ``open()`` to open an on-disk package or ``save()`` to
        save an in-memory Office document.
        
        """
        super(Package, self).__init__()
        self.__parts = PartCollection(self)
        self.__relsitem = None
    
    def loadparts(self, fs, cti, rels):
        """
        Load package parts by walking the relationship graph. If called with
        the package relationships, all parts in the package will be loaded.
        
        rels
           List of :class:`Relationship`. If not already loaded, the target
           part of each relationship is loaded and :meth:`loadparts()` called
           recursively with that part's relationships.
        """
        for r in rels:
            partname = r.target_partname
            # only visit each part once, graph is cyclical
            if partname in self.parts:
                continue
            ct = cti[partname]
            part = self.parts.loadpart(fs, partname, ct)
            # recurse on new part's relationships
            if part.relationships:
                self.loadparts(fs, cti, part.relationships)
    
    def open(self, path):
        """
        Load the on-disk package located at *path*.
        
        """
        fs = FileSystem(path)
        cti = ContentTypesItem().load(fs)
        self.__relsitem = PackageRelationshipsItem().load(fs)
        self.loadparts(fs, cti, self.__relsitem.relationships)
        return self
    
    @property
    def parts(self):
        """
        Return instance of :class:`pptx.packaging.PartCollection` in which
        this package's parts are contained.
        
        """
        return self.__parts
    
    def save(self, path):
        """
        Save this package at *path*.
        
        """
        # NOTE: In real save, first step might be to walk actual parts to make
        #       sure pkg part collection isn't out of date
        # --------------------------------------------------------------------
        path = self.__normalizedpath(path)
        zipfs = ZipFileSystem(path).new()
        cti = ContentTypesItem().compose(self.parts)
        zipfs.write_element(cti.element, '/[Content_Types].xml')
        zipfs.write_element(self.__relsitem.element, '/_rels/.rels')
        for part in self.parts:
            # write part item
            if part.typespec.format == 'xml':
                zipfs.write_element(part.element, part.partname)
            else:
                zipfs.write_blob(part.blob, part.partname)
            # write rels item if part has one
            relsitem = part.relationshipsitem
            if relsitem:
                zipfs.write_element(relsitem.element, relsitem.itemURI)
    
    def __normalizedpath(self, path):
        """
        Add '.pptx' extension to path if not already there.
        
        """
        return path if path.endswith('.pptx') else '%s.pptx' % path
    


# ============================================================================
# FileSystem Classes
# ============================================================================

class FileSystem(object):
    """
    Factory for filesystem interface instances.
    
    A FileSystem object provides access to on-disk package items via their URI
    (e.g. ``/_rels/.rels`` or ``/ppt/presentation.xml``). This allows parts to
    be accessed directly by part name, which for a part is identical to its
    item URI. The complexities of translating URIs into file paths or zip item
    names, and file and zip file access specifics are all hidden by the
    filesystem class. :class:`FileSystem` acts as the Factory, returning the
    appropriate concrete filesystem class depending on what it finds at
    *path*.
    
    """
    def __new__(cls, path):
        # if path is a directory, return instance of DirectoryFileSystem
        if os.path.isdir(path):
            instance = DirectoryFileSystem(path)
        # if path is a zip file, return instance of ZipFileSystem
        elif os.path.isfile(path) and zipfile.is_zipfile(path):
            instance = ZipFileSystem(path)
        # otherwise, something's not right, throw exception
        else:
            raise PackageNotFoundError("""Package not found at %s""" % (path))
        return instance
    

class BaseFileSystem(object):
    """
    Base class for FileSystem classes, providing common methods.
    
    """
    def __init__(self, path):
        self.__path = os.path.abspath(path)
    
    def __contains__(self, itemURI):
        """
        Allows use of 'in' operator to test whether an item with the specified
        URI exists in this filesystem.
        
        """
        return itemURI in self.itemURIs
    
    def getblob(self, itemURI):
        """
        Return byte string of item identified by *itemURI*.
        
        """
        if itemURI not in self:
            raise LookupError("No package item with URI '%s'" % itemURI)
        stream = self.getstream(itemURI)
        blob = stream.read()
        stream.close()
        return blob
    
    def getelement(self, itemURI):
        """
        Return ElementTree element of XML item identified by *itemURI*.
        
        """
        if itemURI not in self:
            raise LookupError("No package item with URI '%s'" % itemURI)
        stream = self.getstream(itemURI)
        try:
            parser = etree.XMLParser(remove_blank_text=True)
            element = etree.parse(stream, parser).getroot()
        except etree.XMLSyntaxError:
            raise NotXMLError("package item %s is not XML" % itemURI)
        stream.close()
        return element
    
    @property
    def path(self):
        """
        Path property is read-only. Need to instantiate a new FileSystem
        object to access a different package.
        """
        return self.__path
    

class DirectoryFileSystem(BaseFileSystem):
    """
    Provides access to package members that have been expanded into an on-disk
    directory structure.
    
    Inherits __contains__(), getelement(), and path from BaseFileSystem.
    
    """
    def __init__(self, path):
        """
        path
           Path to directory containing expanded package.
        
        """
        super(DirectoryFileSystem, self).__init__(path)
    
    def getstream(self, itemURI):
        """
        Return file-like object containing package item identified by
        *itemURI*. Remember to call close() on the stream when you're done
        with it to free up the memory it uses.
        
        """
        if itemURI not in self:
            raise LookupError("No package item with URI '%s'" % itemURI)
        path = os.path.join(self.path, itemURI[1:])
        with open(path) as f:
            stream = StringIO(f.read())
        return stream
    
    @property
    def itemURIs(self):
        """
        Return list of all filenames under filesystem root directory,
        formatted as item URIs. Each URI is the relative path of that file
        with a leading slash added, e.g. '/ppt/slides/slide1.xml'. Although
        not strictly necessary, the results are sorted for neatness' sake.
        
        """
        itemURIs = []
        for dirpath, dirnames, filenames in os.walk(self.path):
            for filename in filenames:
                item_path = os.path.join(dirpath, filename)
                itemURI = item_path[len(self.path):]  # leaves a leading slash on
                itemURIs.append(itemURI)
        return sorted(itemURIs)
    

class ZipFileSystem(BaseFileSystem):
    """
    FileSystem interface for zip-based packages (i.e. regular Office files).
    Provides standard access methods to hide complexity of dealing with
    varied package formats.
    
    Inherits __contains__(), getelement(), and path from BaseFileSystem.
    
    """
    def __init__(self, path):
        """
        path
           Path to directory containing expanded package.
        
        """
        super(ZipFileSystem, self).__init__(path)
    
    def getstream(self, itemURI):
        """
        Return file-like object containing package item identified by
        *itemURI*. Remember to call close() on the stream when you're done
        with it to free up the memory it uses.
        
        """
        if itemURI not in self:
            raise LookupError("No package item with URI '%s'" % itemURI)
        membername = itemURI[1:]  # trim off leading slash
        zip = zipfile.ZipFile(self.path)
        stream = StringIO(zip.read(membername))
        zip.close()
        return stream
    
    @property
    def itemURIs(self):
        """
        Return list of archive members formatted as item URIs. Each member
        name is the archive-relative path of that file. A forward-slash is
        prepended to form the URI, e.g. '/ppt/slides/slide1.xml'. Although
        not strictly necessary, the results are sorted for neatness' sake.
        
        """
        zip = zipfile.ZipFile(self.path)
        namelist = zip.namelist()
        zip.close()
        # zip archive can contain entries for directories, so get rid of those
        itemURIs = [('/%s' % name) for name in namelist if not name.endswith('/')]
        return sorted(itemURIs)
    
    def new(self):
        """
        Create a new zip archive at ``self.path``. If a file by that name
        already exists, it is truncated. Returns self reference to allow
        generative call.
        
        """
        zip = zipfile.ZipFile(self.path, 'w',
                              compression=zipfile.ZIP_DEFLATED)
        zip.close()
        return self
    
    def write_blob(self, blob, itemURI):
        """
        Write *blob* to zip file as binary stream named *itemURI*.
        
        """
        if itemURI in self:
            tmpl = "Item with URI '%s' already in package"
            raise DuplicateKeyError(tmpl % itemURI)
        membername = itemURI[1:]  # trim off leading slash
        zip = zipfile.ZipFile(self.path, 'a',
                              compression=zipfile.ZIP_DEFLATED)
        zip.writestr(membername, blob)
        zip.close()
    
    def write_element(self, element, itemURI):
        """
        Write *element* to zip file as an XML document named *itemURI*.
        
        """
        if itemURI in self:
            tmpl = "Item with URI '%s' already in package"
            raise DuplicateKeyError(tmpl % itemURI)
        membername = itemURI[1:]  # trim off leading slash
        xml = etree.tostring(element, encoding='UTF-8', pretty_print=True,
                             standalone=True)
        xml = util.prettify_nsdecls(xml)
        zip = zipfile.ZipFile(self.path, 'a',
                              compression=zipfile.ZIP_DEFLATED)
        zip.writestr(membername, xml)
        zip.close()
    


# ============================================================================
# Part Type Specs
# ============================================================================

class PartTypeSpec(object):
    """
    Reference to the characteristics of the various part types, as defined in
    ECMA-376.
    
    Entries are keyed by content type, a MIME type-like string that
    distinguishes parts of different types. The content type of each part in a
    package is indicated in the [Content_Types].xml stream located in the root
    of the package.
    
    Instances are cached, so no more than one instance for a particular
    content type is in memory.
    
    .. attribute:: content_type
       
       MIME-type-like string that distinguishes the content of parts of this
       type from simple XML. For example, the content_type of a theme part is
       ``application/vnd.openxmlformats-officedocument.theme+xml``. Each
       part's content type is written in the content types item located in the
       root directory of the package ([Content_Types].xml).
    
    .. attribute:: basename
       
       The root of the part's filename within the package. For example,
       rootname for slideLayout1.xml is 'slideLayout'. Note that the part's
       rootname is also used as its key value.
    
    .. attribute:: ext
       
       The extension of the part's filename within the package. For example,
       file_ext for the presentation part (presentation.xml) is 'xml'.
    
    .. attribute:: cardinality
       
       One of 'single' or 'multiple', specifying whether the part is a
       singleton or tuple within the package. ``presentation.xml`` is an
       example of a singleton part. ``slideLayout4.xml`` is an example of a
       tuple part. The term *tuple* in this context is drawn from set theory
       in math and has no direct relationship to the Python tuple class.
    
    .. attribute:: required
       
       Boolean expressing whether at least one instance of this part type must
       appear in the package. ``presentation`` is an example of a required
       part type. ``notesMaster`` is an example of a optional part type.
    
    .. attribute:: baseURI
       
       The package-relative path of the directory in which part files for this
       type are stored. For example, location for ``slideLayout`` is
       '/ppt/slideLayout'. The leading slash corresponds to the root of the
       package (zip file). Note that directories in the actual package zip
       file do not contain this leading slash (otherwise they would be
       placed in the root directory when the zip file was expanded).
    
    .. attribute:: has_rels
       
       One of 'always', 'never', or 'optional', indicating whether parts of
       this type have a corresponding relationship item, or "rels file".
    
    .. attribute:: rel_type
       
       A URL that identifies this part type in rels files. For example,
       relationshiptype for ``slides/slide1.xml`` is
       ``http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide``
    
    .. attribute:: format
       
       One of 'xml' or 'binary'.
    
    """
    __instances = {}
    __loadclassmap = {}
    __defloadclass = None
    
    def __new__(cls, content_type):
        """
        Only create new instance on first call for content_type. After that,
        use cached instance.
        
        """
        # if there's not an matching instance in the cache, create one
        if content_type not in cls.__instances:
            inst = super(PartTypeSpec, cls).__new__(cls)
            cls.__instances[content_type] = inst
        # return the instance; note that __init__() gets called either way
        return cls.__instances[content_type]
    
    def __init__(self, content_type):
        """
        Initialize spec attributes from constant values in pptx.spec.
        
        """
        # skip loading if this instance is from the cache
        if hasattr(self, '_loaded'):
            return
        # otherwise initialize new instance
        self._loaded = True
        if content_type not in pptx.spec.pml_parttypes:
            tmpl = "no content type '%s' in pptx.spec.pml_parttypes"
            raise KeyError(tmpl % content_type)
        ptsdict = pptx.spec.pml_parttypes[content_type]
        # load attributes from spec constants dictionary
        self.content_type = content_type            # e.g. 'application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml'
        self.basename     = ptsdict['basename']     # e.g. 'slideMaster'
        self.ext          = ptsdict['ext']          # e.g. '.xml'
        self.cardinality  = ptsdict['cardinality']  # e.g. 'single' or 'multiple'
        self.required     = ptsdict['required']     # e.g. False
        self.baseURI      = ptsdict['baseURI']      # e.g. '/ppt/slideMasters'
        self.has_rels     = ptsdict['has_rels']     # e.g. 'always', 'never', or 'optional'
        self.rel_type     = ptsdict['rel_type']     # e.g. 'http://schemas.openxmlformats.org/officedocument/2006/relationships/metadata/core-properties'
        # set class to load parts of this type
        if content_type in PartTypeSpec.__loadclassmap:
            self.loadclass = PartTypeSpec.__loadclassmap[content_type]
        else:
            self.loadclass = PartTypeSpec.__defloadclass
    
    @classmethod
    def register(cls, loadclassmap):
        for content_type, loadclass in loadclassmap.items():
            cls.__loadclassmap[content_type] = loadclass
    
    @classmethod
    def register_def_loadclass(cls, defloadclass):
        cls.__defloadclass = defloadclass
    
    @property
    def format(self):
        """
        One of 'xml' or 'binary'.
        
        """
        return 'xml' if self.ext == '.xml' else 'binary'
    


# ============================================================================
# Package Items
# ============================================================================

class ContentTypesItem(object):
    """
    Development content types item, planned to merge with OldContentTypesItem
    once it's further along and its test suite is reasonably well-developed.
    
    Lookup content type by part name using dictionary syntax, e.g.
    ``content_type = cti['/ppt/presentation.xml']``.
    
    """
    def __init__(self):
        super(ContentTypesItem, self).__init__()
        self.__defaults = None
        self.__overrides = None
    
    def __getitem__(self, partname):
        """
        Return the content type for the part with *partname*.
        
        """
        # throw exception if called before load()
        if self.__defaults is None or self.__overrides is None:
            raise LookupError("""No part name '%s' in [Content_Types].xml""" % (partname))
        # first look for an explicit content type
        if partname in self.__overrides:
            return self.__overrides[partname]
        # if not, look for a default based on the extension
        ext = os.path.splitext(partname)[1]            # get extension of partname
        ext = ext[1:] if ext.startswith('.') else ext  # with leading dot trimmed off
        if ext in self.__defaults:
            return self.__defaults[ext]
        # if neither of those work, throw an exception
        raise LookupError("""No part name '%s' in package content types item""" % (partname))
    
    def __len__(self):
        """
        Return sum count of Default and Override elements.
        
        """
        count = len(self.__defaults) if self.__defaults is not None else 0
        count += len(self.__overrides) if self.__overrides is not None else 0
        return count
    
    def compose(self, parts):
        """
        Assemble a [Content_Types].xml item based on the contents of *parts*.
        
        """
        # extensions in this dict includes leading '.'
        def_cts = pptx.spec.default_content_types
        # initialize working dictionaries for defaults and overrides
        self.__defaults = {ext[1:]: def_cts[ext] for ext in ('.rels', '.xml')}
        self.__overrides = {}
        # compose appropriate element for each part
        for part in parts:
            ext = os.path.splitext(part.partname)[1]
            # if extension is '.xml', assume an override. There might be a
            # fancier way to do this, otherwise I don't know what 'xml'
            # Default entry is for.
            if ext == '.xml':
                self.__overrides[part.partname] = part.typespec.content_type
            elif ext in def_cts:
                self.__defaults[ext[1:]] = def_cts[ext]
            else:
                tmpl = "extension '%s' not found in default_content_types"
                raise LookupError(tmpl % (ext))
        return self
    
    @property
    def element(self):
        nsmap = {None: pptx.spec.nsmap['ct']}
        element = etree.Element(qname('ct', 'Types'), nsmap=nsmap)
        if self.__defaults:
            for ext in sorted(self.__defaults.keys()):
                subelm = etree.SubElement(element, qname('ct', 'Default'))
                subelm.set('Extension', ext)
                subelm.set('ContentType', self.__defaults[ext])
        if self.__overrides:
            for partname in sorted(self.__overrides.keys()):
                subelm = etree.SubElement(element, qname('ct', 'Override'))
                subelm.set('PartName', partname)
                subelm.set('ContentType', self.__overrides[partname])
        return element
    
    def load(self, fs):
        """
        Retrieve [Content_Types].xml from specified file system and load it.
        Returns a reference to this ContentTypesItem instance to allow
        generative call, e.g. ``cti = ContentTypesItem().load(fs)``.
        
        """
        element = fs.getelement('/[Content_Types].xml')
        defaults = element.findall(qname('ct','Default'))
        overrides = element.findall(qname('ct','Override'))
        self.__defaults = {d.get('Extension'): d.get('ContentType') for d in defaults}
        self.__overrides = {o.get('PartName'): o.get('ContentType') for o in overrides}
        return self
    


# ============================================================================
# Relationship-related Classes
# ============================================================================

class Relationship(object):
    """
    In-memory relationship between a source item (usually a part) and a target
    part.
    
    Requires access to a parent object which must provide certain properties
    Relationship needs to form proper URIs. The source part (from-part) in the
    relationship is implictly the part that's keeping track of this instance,
    generally the part having the RelationshipCollection this Relationship
    stores a reference to in its *parent* attribute.
    
    """
    def __init__(self, parent, element):
        super(Relationship, self).__init__()
        self.parent = parent  # parent is generally a RelationshipCollection instance
        self.rId = element.get('Id')
        self.reltype = element.get('Type')
        self.__target = element.get('Target')
    
    @property
    def element(self):
        element = etree.Element('Relationship')
        element.set('Id', self.rId)
        element.set('Type', self.reltype)
        element.set('Target', self.__target)
        return element
    
    @property
    def target(self):
        """
        Return relative URI to target part, suitable for use in a rels file.
        
        This attribute is read-only on :class:`Relationship`.
        
        """
        return self.__target
    
    @property
    def target_partname(self):
        """
        Return part name of target part.
        
        In rels files, the relationship target is expressed as a relative URI,
        essentially a relative path to the target part from the source part's
        directory. This method performs the calculations to translate that
        relative URI into an absolute one, which is what the part name is.
        
        """
        return os.path.abspath(os.path.join(self.parent.baseURI, self.__target))
    

class RelationshipCollection(list):
    """
    Provides collection and convenience methods and properties to the list of
    relationships belonging to a package or part.
    
    Each relationship has a local id (rId), source, target, and relationship
    type. The source is implicitly the part or package the relationship
    collection belongs to, so the RelationshipCollection needs a reference to
    the source on construction.
    
    """
    def __init__(self, parent):
        """
        parent
           So far, an instance of a subclass of :class:`RelationshipsItem`
           that can provide delegated attributes such as *baseURI*.
        
        """
        super(RelationshipCollection, self).__init__()
        self.__parent = parent
        self._id_dict = {}
    
    def __repr__(self):
        mod = self.__class__.__module__
        cls = self.__class__.__name__
        mem = '0x' + hex(id(self))[2:].zfill(8).upper()
        return '<{0}.{1} instance at {2}>'.format(mod, cls, mem)
    
    def additem(self, rel_elm):
        """
        Construct a new Relationship instance from *rel_elm* and append it to
        the collection.
        
        """
        rel = Relationship(self, rel_elm)
        if rel.rId in self._id_dict:
            msg = "cannot add relationship with duplicate key ('%s')" % rel.rId
            raise DuplicateKeyError(msg)
        self._id_dict[rel.rId] = rel
        self.append(rel)
        return rel
    
    @property
    def baseURI(self):
        """
        Return the baseURI used to resolve target URIs in this collection into
        partnames.
        
        """
        return self.__parent.baseURI
    
    def getitem(self, key):
        """
        Return the Relationship instance corresponding to *key*, the rId value
        of the relationship to return.
        
        """
        if key in self._id_dict:
            return self._id_dict[key]
        raise LookupError("""getitem() lookup in %s failed with key '%s'""" % (self.__class__.__name__, key))
    

class RelationshipsItem(object):
    """
    Relationships items specify the relationships between parts of the
    package, although they are not themselves a part. All relationship items
    are XML documents having a filename with the extension '.rels' located in
    a directory named '_rels' located in the same directory as the part. The
    package relationship item has the URI '/_rels/.rels'. Part relationship
    items have the same filename as the part whose relationships they
    describe, with the '.rels' extension appended as a suffix. For example,
    the relationship item for a part named 'slide1.xml' would have the URI
    '/ppt/slides/_rels/slide1.xml.rels'.
    
    """
    def __init__(self):
        super(RelationshipsItem, self).__init__()
        self.__relationships = RelationshipCollection(self)
    
    @property
    def element(self):
        nsmap = {None: pptx.spec.nsmap['pr']}
        element = etree.Element(qname('pr', 'Relationships'), nsmap=nsmap)
        for rel in self.__relationships:
            element.append(rel.element)
        return element
    
    def load(self, fs, itemURI):
        """
        Load contents of rels file specified by *itemURI* from filesystem
        provided. Returns a reference to this RelationshipsItem instance to
        enable generative call, e.g.
        ``ri = RelationshipsItem().load(stream)``. Discards any existing
        relationships before loading.
        
        """
        element = fs.getelement(itemURI)
        relationships = element.findall(qname('pr','Relationship'))
        # discard relationships from any prior load
        self.__relationships = RelationshipCollection(self)
        for r in relationships:
            self.__relationships.additem(r)
        return self
    
    @property
    def relationships(self):
        """
        Return instance of :class:`RelationshipsCollection` containing the
        relationships in this :class:`RelationshipsItem`.
        
        """
        return self.__relationships
    

class PackageRelationshipsItem(RelationshipsItem):
    """
    Differentiated behaviors for the package relationships item. The package
    relationship item is a singleton, and has other differences from part
    relationships items.
    
    """
    def __init__(self):
        super(PackageRelationshipsItem, self).__init__()
    
    @property
    def baseURI(self):
        """
        Return the baseURI used to resolve target URIs into partnames for
        relationships in the package relationships item.
        
        """
        return '/'
    
    @property
    def itemURI(self):
        """
        Return the package item URI of this relationships item.
        
        """
        return '/_rels/.rels'
    
    def load(self, fs):
        """
        Load contents of rels file specified by *itemURI* from filesystem
        provided. Returns a reference to this RelationshipsItem instance to
        enable generative call, e.g.
        ``ri = RelationshipsItem().load(stream)``. Discards any existing
        relationships before loading.
        
        """
        return super(PackageRelationshipsItem, self).load(fs, self.itemURI)
    

class PartRelationshipsItem(RelationshipsItem):
    """
    Differentiated behaviors for part relationships items.
    
    .. attribute:: baseURI
       
       baseURI used to resolve target URIs into partnames for this
       relationships item.
    
    """
    def __init__(self):
        super(PartRelationshipsItem, self).__init__()
        self.__part = None
        self.baseURI = None
        self.__itemURI = None
    
    @property
    def itemURI(self):
        """
        Return the package item URI of this relationships item.
        
        """
        return self.__itemURI
    
    def load(self, part, fs, itemURI):
        """
        Load contents of rels file specified by *itemURI* from filesystem
        provided. Returns a reference to this RelationshipsItem instance to
        enable generative call, e.g.
        ``ri = RelationshipsItem().load(stream)``. Discards any existing
        relationships before loading.
        
        """
        if not isinstance(part, Part):
            msg = "'part' parameter of invalid type; expected 'Part', "\
                  "got '%s'" % (type(part).__name__)
            raise TypeError(msg)
        self.__part = part
        self.baseURI = os.path.split(part.itemURI)[0]
        self.__itemURI = itemURI
        return super(PartRelationshipsItem, self).load(fs, itemURI)
    


# ============================================================================
# Parts
# ============================================================================

class PartCollection(list):
    """
    Provides collection and convenience methods and properties for list of
    package parts.
    
    """
    def __init__(self, package):
        super(PartCollection, self).__init__()
        self.__partname_dict = {}
        self.__package = package
    
    def __contains__(self, partname):
        """
        Return True if part with *partname* is member of collection.
        
        """
        return partname in self.__partname_dict
    
    def __repr__(self):
        mod = self.__class__.__module__
        cls = self.__class__.__name__
        mem = '0x' + hex(id(self))[2:].zfill(8).upper()
        return '<{0}.{1} instance at {2}>'.format(mod, cls, mem)
    
    def getitem(self, partname):
        """
        Return the part with URI *partname*.
        
        """
        if partname in self.__partname_dict:
            return self.__partname_dict[partname]
        raise LookupError("getitem() lookup in %s failed with partname '%s'"
                           % (self.__class__.__name__, partname))
    
    def loadpart(self, fs, partname, content_type):
        """
        Create new part and add it to collection.
        
        :param fs: Filesystem from which to load part
        :type  fs: :class:`FileSystem`
        :param partname: Package item URI (part name) of part to be loaded.
        :param typespec: Metadata for parts of this type
        :type  typespec: :class:`PartTypeSpec`
        :param content_type: content type of part identified by *partname*
        
        """
        if partname in self.__partname_dict:
            msg = "cannot add part with duplicate key ('%s')" % partname
            raise DuplicateKeyError(msg)
        part = Part().load(fs, partname, content_type)
        self.__partname_dict[partname] = part
        self.append(part)
        return part
    
    @property
    def package(self):
        """
        Package this part collection belongs to.
        
        """
        return self.__package
    

class Part(object):
    """
    Package Part.
    
    """
    def __init__(self):
        super(Part, self).__init__()
        self.__partname = None
        self.__relsitem = None
        self.blob = None
        self.element = None
        self.typespec = None
    
    def __relsitemURI(self, typespec, partname, fs):
        """
        Return package URI for this part's relationships item. Returns None if
        a part of this type never has relationships. Also returns None if a
        part of this type has only optional relationships and the package
        contains no rels item for this part.
        
        """
        if typespec.has_rels == 'never':
            return None
        head, tail = os.path.split(partname)
        relsitemURI = '%s/_rels/%s.rels' % (head, tail)
        present = relsitemURI in fs
        if typespec.has_rels == 'optional':
            return relsitemURI if present else None
        return relsitemURI
    
    @property
    def itemURI(self):
        """
        Return item URI for this part. For a part, the item URI is equivalent
        to its part name.
        
        """
        return self.__partname
    
    def load(self, fs, partname, content_type):
        """
        Load part from filesystem.
        
        :param fs: Filesystem from which to load part
        :type  fs: :class:`FileSystem`
        :param partname: Package item URI (part name) of part to be loaded.
        :param typespec: Metadata from spec on type of part to be loaded.
        :type  typespec: :class:`PartTypeSpec`
        :param content_type: content type of part identified by *partname*
        
        """
        # set persisted attributes
        self.typespec = PartTypeSpec(content_type)
        self.__partname = partname
        
        # load element if part is xml, blob otherwise
        self.blob = None     # discard from any prior load
        self.element = None  # discard from any prior load
        if self.typespec.format == 'xml':
            self.element = fs.getelement(partname)
        else:
            # REFACTOR: add DirFileSystem.getblob() and use fs.getblob instead
            stream = fs.getstream(partname)
            self.blob = stream.read()
            stream.close()
        
        # load relationships item if part has one
        self.__relsitem = None  # discard any from last load
        relsitemURI = self.__relsitemURI(self.typespec, partname, fs)
        if relsitemURI:
            if relsitemURI not in fs:
                tmpl = "required relationships item '%s' not found in package"
                raise CorruptedPackageError(tmpl % relsitemURI)
            self.__relsitem = PartRelationshipsItem()
            self.__relsitem.load(self, fs, relsitemURI)
        
        return self
        
        # # load the model class for this part if one is specified
        # if self.typespec.loadclass:
        #     self.typespec.loadclass(self)
    
    @property
    def partname(self):
        """
        Package item URI for this part, commonly known as its part name.
        
        """
        return self.__partname
    
    @property
    def relationshipsitem(self):
        """
        Return reference to this part's relationships item, or None if it
        doesn't have one. A part with no relationships has no relationships
        item.
        
        """
        return self.__relsitem
    
    @property
    def relationships(self):
        """
        Return instance of :class:`RelationshipCollection` containing the
        relationships for this part.
        
        """
        if self.__relsitem is None:
            return None
        return self.__relsitem.relationships
    

