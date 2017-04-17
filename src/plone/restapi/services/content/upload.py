# -*- coding: utf-8 -*-
from Products.CMFCore.utils import getToolByName
from Products.CMFPlone.utils import base_hasattr
from base64 import b64decode
from email.utils import formatdate
from fnmatch import fnmatch
from plone.app.textfield.interfaces import IRichText
from plone.app.textfield.value import RichTextValue
from plone.namedfile.interfaces import INamedField
from plone.rest.interfaces import ICORSPolicy
from plone.restapi.services import Service
from plone.restapi.services.content.utils import create
from plone.restapi.services.content.utils import rename
from plone.rfc822.interfaces import IPrimaryFieldInfo
from uuid import uuid4
from zope.component import queryMultiAdapter
from zope.interface import implements
from zope.publisher.interfaces import IPublishTraverse
from zope.publisher.interfaces import NotFound

import json
import os
import time

TUS_OPTIONS_RESPONSE_HEADERS = {
    'Tus-Resumable': '1.0.0',
    'Tus-Version': '1.0.0',
    'Tus-Extension': 'creation,expiration',
}


class UploadOptions(Service):
    """TUS upload endpoint for handling OPTIONS requests without CORS."""

    def reply(self):
        for name, value in TUS_OPTIONS_RESPONSE_HEADERS.items():
                    self.request.response.setHeader(name, value)
        return super(UploadOptions, self).reply()


class TUSBaseService(Service):

    def __call__(self):
        # We need to add additional TUS headers if this is a CORS preflight
        # request.
        policy = queryMultiAdapter((self.context, self.request), ICORSPolicy)
        if policy is not None:
            if self.request._rest_cors_preflight:
                policy.process_preflight_request()
                # Add TUS options headers in addition to CORS headers
                for name, value in TUS_OPTIONS_RESPONSE_HEADERS.items():
                    self.request.response.setHeader(name, value)
                return

        return self.render()

    def check_tus_version(self):
        version = self.request.getHeader('Tus-Resumable')
        if version != '1.0.0':
            return False
        return True

    def unsupported_version(self):
        self.request.response.setHeader('Tus-Version', '1.0.0')
        self.request.response.setStatus(412)
        return {'error': {'type': 'Precondition Failed',
                          'message': 'Unsupported version'}}

    def error(self, type, message, status=400):
        """
        Set a status code (400 is the default error in the TUS
        reference server implementation) and return a plone.restapi
        conform error body.
        """
        self.request.response.setStatus(status)
        return {'error': {
                'type': type,
                'message': message,
                }}


class UploadPost(TUSBaseService):
    """TUS upload endpoint for creating a new upload resource."""

    def reply(self):
        if not self.check_tus_version():
            return self.unsupported_version()

        length = self.request.getHeader('Upload-Length', '')
        try:
            length = int(length)
        except ValueError:
            return self.error('Bad Request',
                              'Missing or invalid Upload-Length header')

        # Parse metadata
        metadata = {}
        for item in self.request.getHeader('Upload-Metadata', '').split(','):
            key_value = item.split()
            if len(key_value) == 2:
                metadata[key_value[0].lower()] = b64decode(key_value[1])
        metadata['length'] = length

        tus_upload = TUSUpload(uuid4().hex, metadata=metadata)

        self.request.response.setStatus(201)
        self.request.response.setHeader('Location', '{}/@upload/{}'.format(
            self.context.absolute_url(), tus_upload.uid))
        self.request.response.setHeader('Upload-Expires', tus_upload.expires())


class UploadFileBase(TUSBaseService):
    implements(IPublishTraverse)

    def __init__(self, context, request):
        super(UploadFileBase, self).__init__(context, request)
        self.uid = None

    def publishTraverse(self, request, name):
        if self.uid is None:
            self.uid = name
        else:
            raise NotFound(self, name, request)
        return self

    def tus_upload(self):
        if self.uid is None:
            return None

        tus_upload = TUSUpload(self.uid)
        length = tus_upload.length()
        if length == 0:
            return None

        return tus_upload


class UploadHead(UploadFileBase):
    """TUS upload endpoint for handling HEAD requests"""

    def reply(self):

        tus_upload = self.tus_upload()
        if tus_upload is None:
            return self.error('Not Found', '', 404)

        if not self.check_tus_version():
            return self.unsupported_version()

        self.request.response.setHeader('Upload-Length', '{}'.format(
            tus_upload.length()))
        self.request.response.setHeader('Upload-Offset', '{}'.format(
            tus_upload.offset()))
        self.request.response.setHeader('Tus-Resumable', '1.0.0')
        self.request.response.setHeader('Cache-Control', 'no-store')
        self.request.response.setStatus(200, lock=1)
        return super(UploadHead, self).reply()


class UploadPatch(UploadFileBase):
    """TUS upload endpoint for handling PATCH requests"""

    implements(IPublishTraverse)

    def reply(self):

        tus_upload = self.tus_upload()
        if tus_upload is None:
            return self.error('Not Found', '', 404)

        if not self.check_tus_version():
            return self.unsupported_version()

        content_type = self.request.getHeader('Content-Type')
        if content_type != 'application/offset+octet-stream':
            return self.error(
                'Bad Request', 'Missing or invalid Content-Type header')

        offset = self.request.getHeader('Upload-Offset', '')
        try:
            offset = int(offset)
        except ValueError:
            return self.error(
                'Bad Request', 'Missing or invalid Upload-Offset header')

        tus_upload.write(self.request._file, offset)

        if tus_upload.finished:
            offset = tus_upload.offset()
            metadata = tus_upload.metadata()
            filename = metadata.get('filename', '')
            content_type = metadata.get('content-type',
                                        'application/octet-stream')
            type_ = metadata.get('@type')
            if type_ is None:
                ctr = getToolByName(self.context, 'content_type_registry')
                type_ = ctr.findTypeName(
                    filename.lower(), content_type, '') or 'File'

            obj = create(self.context, type_)

            # Get fieldname of file object
            fieldname = metadata.get('fieldname')

            # Update field with file data
            with open(tus_upload.filepath, 'rb') as f:
                # No field name given, update primary field
                if not fieldname:
                    info = IPrimaryFieldInfo(obj, None)
                    if info is not None:
                        field = info.field
                        if IRichText.providedBy(field):
                            value = f.read()
                            field.set(obj, RichTextValue(
                                raw=value.decode('utf8'),
                                mimeType=(content_type or
                                          field.default_mime_type),
                                outputMimeType=field.output_mime_type))
                        elif INamedField.providedBy(field):
                            field.set(obj,
                                      field._type(data=f,
                                                  filename=filename,
                                                  contentType=content_type))
                        else:
                            # TODO: handle unsupported field type
                            self.request.response.setStatus(501, lock=1)
                            raise NotImplementedError('Not Implemented')
                    elif base_hasattr(obj, 'getPrimaryField'):
                        field = obj.getPrimaryField()
                        mutator = field.getMutator(obj)
                        mutator(f,
                                content_type=content_type,
                                filename=filename)
                else:
                    # TODO: handle non-primary fields
                    self.request.response.setStatus(501, lock=1)
                    raise NotImplementedError('Not Implemented')

            rename(obj)
            tus_upload.cleanup()
            self.request.response.setHeader('Location', obj.absolute_url())
        else:
            offset = tus_upload.offset()
            self.request.response.setHeader(
                'Upload-Expires', tus_upload.expires())

        self.request.response.setHeader('Tus-Resumable', '1.0.0')
        self.request.response.setHeader('Upload-Offset', '{}'.format(offset))
        self.request.response.setStatus(204, lock=1)
        return super(UploadPatch, self).reply()


class TUSUpload(object):

    file_prefix = 'tus_upload_'
    expiration_period = 60 * 60
    finished = False

    def __init__(self, uid, metadata=None):
        self.uid = uid

        self.tmp_dir = os.environ.get('TUS_TMP_FILE_DIR')
        if self.tmp_dir is None:
            client_home = os.environ.get('CLIENT_HOME')
            self.tmp_dir = os.path.join(client_home, 'tus-uploads')
        if not os.path.isdir(self.tmp_dir):
            os.makedirs(self.tmp_dir)

        self.filepath = os.path.join(self.tmp_dir, self.file_prefix + self.uid)
        self.metadata_path = self.filepath + '.json'
        self._metadata = None

        if metadata is not None:
            self.initalize(metadata)

    def initalize(self, metadata):
        """Initialize a new TUS upload by writing its metadata to disk."""
        self.cleanup_expired()
        with open(self.metadata_path, 'wb') as f:
            json.dump(metadata, f)

    def length(self):
        """Returns the total upload length."""
        metadata = self.metadata()
        if 'length' in metadata:
            return metadata['length']
        return 0

    def offset(self):
        """Returns the current offset."""
        if os.path.exists(self.filepath):
            return os.path.getsize(self.filepath)
        return 0

    def write(self, infile, offset=0):
        """Write to uploaded file at the given offset."""
        mode = 'wb'
        if os.path.exists(self.filepath):
            mode = 'ab+'
        with open(self.filepath, mode) as f:
            f.seek(offset)
            while True:
                chunk = infile.read(2 << 16)
                if not chunk:
                    offset = f.tell()
                    break
                f.write(chunk)
        length = self.length()
        if length and offset >= length:
            self.finished = True

    def metadata(self):
        """Returns the metadata of the current upload."""
        if self._metadata is None:
            if os.path.exists(self.metadata_path):
                with open(self.metadata_path, 'rb') as f:
                    self._metadata = json.load(f)
        return self._metadata or {}

    def cleanup(self):
        """Remove temporary upload files."""
        if os.path.exists(self.filepath):
            os.remove(self.filepath)
        if os.path.exists(self.metadata_path):
            os.remove(self.metadata_path)

    def cleanup_expired(self):
        """Cleanup unfinished uploads that have expired."""
        for filename in os.listdir(self.tmp_dir):
            if fnmatch(filename, 'tus_upload_*.json'):
                metadata_path = os.path.join(self.tmp_dir, filename)
                filepath = metadata_path[:-5]
                if os.path.exists(filepath):
                    mtime = os.stat(filepath).st_mtime
                else:
                    mtime = os.stat(metadata_path).st_mtime

                if (time.time() - mtime) > self.expiration_period:
                    os.remove(metadata_path)
                    if os.path.exists(filepath):
                        os.remove(filepath)

    def expires(self):
        """Returns the expiration time of the current upload."""
        if os.path.exists(self.filepath):
            expiration = os.stat(
                self.filepath).st_mtime + self.expiration_period
        else:
            expiration = time.time() + self.expiration_period
        return formatdate(expiration, False, True)
