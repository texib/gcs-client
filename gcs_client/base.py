# -*- coding: utf-8 -*-
# Copyright 2015 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.

from __future__ import absolute_import

import abc
import six

import requests

from gcs_client import common
from gcs_client import errors as gcs_errors


class GCS(object):
    _required_attributes = ['credentials']

    _URL = 'https://www.googleapis.com/storage/v1/b'
    _URL_UPLOAD = 'https://www.googleapis.com/upload/storage/v1/b'

    def __init__(self, credentials, retry_params=None):
        """Base GCS initialization.

        :param credentials: credentials to use for accessing GCS
        :type credentials: Credentials
        :param retry_params: retry configuration used for communications with
                             GCS.  If not specified RetryParams.getdefault()
                             will be used.
        :type retry_params: RetryParams
        :returns: None
        """
        self.credentials = credentials
        self._retry_params = retry_params or common.RetryParams.get_default()

    def _request(self, op='GET', headers=None, body=None, parse=False,
                 ok=(requests.codes.ok,), url=None, format_url=True, **params):
        """Request actions on a GCS resource.

        :param op: Operation to perform (GET, PUT, POST, HEAD, DELETE).
        :type op: six.string_types
        :param headers: Headers to send in the request.  Authentication will be
                        added.
        :type headers: dict
        :param body: Body to send in the request.
        :type body: Dictionary, bytes or file-like object.
        :param parse: If we want to check that response body is JSON.
        :type parse: bool
        :param ok: Response status codes to consider as OK.
        :type ok: Iterable of integer numbers
        :param url: Alternative url to use
        :type url: six.string_types
        :param format_url: If we want provided url to be formatted with params
        :type format_url: bool
        :param params: All params to send as URL params in the request.
        :returns: requests.Request
        :"""
        headers = {} if not headers else headers.copy()
        headers['Authorization'] =\
            'Bearer ' + self.credentials.get_access_token().access_token

        if not url:
            url = self._URL

        if format_url:
            format_args = {
                x: requests.utils.quote(six.text_type(getattr(self, x)),
                                        safe='')
                for x in self._required_attributes}
            url = url.format(**format_args)

        r = requests.request(op, url, params=params, headers=headers,
                             json=body)

        if r.status_code not in ok:
            raise gcs_errors.create_http_exception(r.status_code, r.content)

        if parse:
            try:
                r.json()
            except Exception:
                raise gcs_errors.Error('GCS response is not JSON: %s' %
                                       r.content)

        return r

    @property
    def retry_params(self):
        """Get retry configuration used by this instance for accessing GCS."""
        return self._retry_params

    @retry_params.setter
    def retry_params(self, retry_params):
        """Set retry configuration used by this instance for accessing GCS.

        :param retry_params: Retry configuration used for communications with
                             GCS.  If None is passed retries will be disabled.
        :type retry_params: RetryParams or NoneType
        """
        assert isinstance(retry_params, (type(None), common.RetryParams))
        self._retry_params = retry_params

#     @property
#     def credentials(self):
#         """Credentials used to connect to GCS server."""
#         return self._credentials

#     @credentials.setter
#     def credentials(self, value):
#         if value == getattr(self, '_credentials', not value):
#             return
#         self._credentials = value

    @common.is_complete
    @common.retry
    def exists(self):
        """Check if exists in GCS server."""
        try:
            self._request(op='HEAD')
        except (gcs_errors.NotFound, gcs_errors.BadRequest):
            return False
        return True


class Fillable(GCS):
    def __init__(self, credentials, retry_params=None):
        super(Fillable, self).__setattr__('_gcs_attrs', {})
        # We need to set a default value for _credentials, otherwise we would
        # end up calling __get_attr__ on GCS base class
        self.credentials = credentials
        super(Fillable, self).__init__(credentials, retry_params)
        self._data_retrieved = False
        self._exists = None

    @classmethod
    def _obj_from_data(cls, data, credentials=None, retry_params=None):
        obj = cls(credentials=credentials, retry_params=retry_params)
        obj._fill_with_data(data)
        return obj

    def __getattribute__(self, name):
        gcs_attrs = super(Fillable, self).__getattribute__('_gcs_attrs')
        if name in gcs_attrs:
            return gcs_attrs[name]
        return super(Fillable, self).__getattribute__(name)

    def __getattr__(self, name):
        def attr_error():
            raise AttributeError("'%s' object has no attribute '%s'" %
                                 (self.__class__.__name__, name))

        if self._data_retrieved or self._exists is False:
            attr_error()

        try:
            data = self._get_data()
            self._exists = True
        except gcs_errors.NotFound:
            self._exists = False
            attr_error()

        self._fill_with_data(data)
        return getattr(self, name)

    def __setattr__(self, name, value, force_gcs=False):
        if force_gcs or name in self._gcs_attrs:
            self._gcs_attrs[name] = value
        else:
            super(Fillable, self).__setattr__(name, value)

    def _fill_with_data(self, data):
        self._data_retrieved = True
        for k, v in data.items():
            if isinstance(v, dict) and len(v) == 1:
                if six.PY3:
                    v = tuple(v.values())[0]
                else:
                    v = v.values()[0]
            self.__setattr__(k, v, True)

    def _get_data(self):
        raise NotImplementedError


class Listable(GCS):
    __metaclass__ = abc.ABCMeta

    @common.is_complete
    @common.retry
    def _list(self, _list_url=None, **kwargs):
        # Get url and child class
        if not _list_url:
            _list_url = self._list_url

        # Retrieve the list from GCS
        result = []
        while True:
            # Get the first page of items
            r = self._request(parse=True, url=_list_url, **kwargs).json()

            # Transform data from GCS into classes
            result.extend(gcs_factory(r['kind'], b, self.credentials,
                                      self.retry_params)
                          for b in r.get('items', []))

            if r.get('prefixes'):
                result.extend(gcs_factory('storage#prefix', self.name, prefix,
                                          kwargs.get('delimiter'),
                                          self.credentials,
                                          self.retry_params)
                              for prefix in r.get('prefixes', []))

            kwargs['pageToken'] = r.get('nextPageToken')
            if not kwargs['pageToken']:
                break

        return result

    list = _list

    _list_url = None


def all_subclasses(cls):
    """Get all subclasses for a class recursevely."""
    return cls.__subclasses__() + [subcls for s in cls.__subclasses__()
                                   for subcls in all_subclasses(s)]


gcs_classes = {}


def gcs_factory(kind, *args, **kwargs):
    """Return an instance for a class of kind type."""

    # Build kind to class mapping if it doesn't already exist
    if not gcs_classes:
        subclasses = all_subclasses(GCS)
        for subcls in subclasses:
            # We only map classes that have the kind attribute
            if hasattr(subcls, 'kind'):
                gcs_classes[getattr(subcls, 'kind')] = subcls

    cls = gcs_classes.get(kind)
    # Instantiate the class, if has _obj_from_data method we create the class
    # and then call it with all the arguments and if it doesn't we just
    # instantiate the class with the arguments.
    return getattr(cls, '_obj_from_data', cls)(*args, **kwargs)
