from __future__ import unicode_literals
import logging
import re
import warnings
from collections import OrderedDict

from goblin import connection
from goblin._compat import string_types, print_, add_metaclass
from goblin.tools import import_string
from goblin import properties
from goblin.exceptions import GoblinException, SaveStrategyException, \
    ModelException, ElementDefinitionException
from goblin.gremlin import BaseGremlinMethod
from goblin import connection


logger = logging.getLogger(__name__)

# dict of node and edge types for rehydrating results
vertex_types = {}
edge_types = {}


class BaseElement(object):
    """
    The base model class, don't inherit from this, inherit from Model, defined
    below
    """
    # __enum_id_only__ = True
    FACTORY_CLASS = None

    class DoesNotExist(GoblinException):
        """
        Object not found in database
        """
        pass

    class MultipleObjectsReturned(GoblinException):
        """
        Multiple objects returned on unique key lookup
        """
        pass

    class WrongElementType(GoblinException):
        """
        Unique lookup with key corresponding to vertex of different type
        """
        pass

    def __init__(self, **values):
        """
        Initialize the element with the given properties.

        :param values: The properties for this element
        :type values: dict

        """
        self._id = values.get('id')
        self._label = values.get('label')
        self._values = {}
        self._manual_values = {}
        # print_("Received values: %s" % values)
        # print_("Known Relationships: %s" % self._relationships)
        for name, prop in self._properties.items():
            # print_("trying name: %s in values" % name)
            value = values.get(name, None)
            if value is not None:
                # print_("Got value")
                value = prop.to_python(value)
            # else:
                # print_("no value found")
            value_mngr = prop.value_manager(prop, value, prop.save_strategy)
            self._values[name] = value_mngr
            setattr(self, name, value)

        # unknown properties that are loaded manually
        from goblin.properties.base import BaseValueManager
        for kwarg in set(values.keys()).difference(
                set(self._properties.keys())):  # set(self._properties.keys()) - set(values.keys()):
            if kwarg not in ('id', 'inV', 'outV', 'label'):
                self._manual_values[kwarg] = BaseValueManager(
                    None, values.get(kwarg))

    @property
    def label(self):
        return self._label

    @property
    def id(self):
        return self._id

    @id.setter
    def id(self, id):
        self._id = id

    def __eq__(self, other):
        """
        Check for equality between two elements.

        :param other: Element to be compared to
        :type other: BaseElement
        :rtype: boolean

        """
        if not isinstance(other, BaseElement):  # pragma: no cover
            return False
        return self.as_dict() == other.as_dict()

    def __ne__(self, other):
        """
        Check for inequality between two elements.

        :param other: Element to be compared to
        :type other: BaseElement
        :rtype: boolean

        """
        return not self.__eq__(other)  # pragma: no cover

    @classmethod
    def _type_name(cls, manual_name):
        """
        Returns the element name if it has been defined, otherwise it creates
        it from the module and class name.

        :param manual_name: Name to override the default type name
        :type manual_name: str
        :rtype: str

        """
        pf_name = ''
        if manual_name:
            pf_name = manual_name.lower()
        else:
            camelcase = re.compile(r'([a-z])([A-Z])')
            ccase = lambda s: camelcase.sub(lambda v: '{}_{}'.format(
                v.group(1), v.group(2).lower()), s)

            pf_name += ccase(cls.__name__)
            pf_name = pf_name.lower()
        return pf_name.lstrip('_')

    def validate_field(self, field_name, val):
        """
        Perform the validations associated with the field with the given name
        on the value passed.

        :param field_name: The name of property whose validations will be run
        :type field_name: str
        :param val: The value to be validated
        :type val: mixed

        """
        return self._properties[field_name].validate(val)

    def validate(self):
        """Cleans and validates the field values"""
        for name in self._properties.keys():
            # print_("Validating {}...".format(name))
            func_name = 'validate_{}'.format(name)
            val = getattr(self, name)
            # print_("Got {}: func_name: {}, attr: {} ({})".format(name, func_name, val, type(val)))
            if hasattr(self, func_name):
                # print_("Calling custom function: {}".format(func_name))
                val = getattr(self, func_name)(val)
            else:
                # print_("Calling standard function: {}".format(self._properties[name]))
                val = self._properties[name].validate(val)  # self.validate_field(name, val)
            # print_("Validated {}: val: {} ({}), func_name: {}".format(name, val, type(val), func_name))
            setattr(self, name, val)

    def as_dict(self):
        """
        Returns a map of column names to cleaned values

        :rtype: dict

        """
        values = {}
        for name, prop in self._properties.items():
            values[name] = prop.to_database(getattr(self, name, None))
        values.update(self._manual_values)
        values['id'] = self.id
        return values

    def as_save_params(self):
        """
        Returns a map of property names to cleaned values containing only the
        properties which should be persisted on save.

        :rtype: dict

        """
        values = {}
        was_saved = self._id is not None
        for name, prop in self._properties.items():
            # Determine the save strategy for this column
            prop_strategy = prop.get_save_strategy()

            # Enforce the save strategy
            vm = self._values[name]
            should_save = prop_strategy.condition(
                previous_value=vm.previous_value, value=vm.value,
                has_changed=vm.changed, first_save=was_saved,
                graph_property=prop)

            if should_save:
                # print_("Saving %s to database for name %s" % (prop.db_field_name or name, name))
                values[prop.db_field_name or name] = prop.to_database(vm.value)

        # manual values
        for name, prop in self._manual_values.items():
            if prop is None:
                # Remove this property entirely
                values[name] = None
            else:
                # Determine the save strategy
                prop_strategy = prop.strategy
                if prop_strategy.condition(previous_value=prop.previous_value,
                                           value=prop.value,
                                           has_changed=prop.changed,
                                           first_save=was_saved,
                                           graph_property=None):
                    values[name] = prop.value

        return values

    @classmethod
    def translate_db_fields(cls, data):
        """
        Translates field names from the database into field names used in our
        model this is for cases where we're saving a field under a different
        name than it's model property

        :param data: dict
        :rtype: dict
        """
        dst_data = data.copy().get('properties', {})
        if data.get('label', ''):
            dst_data.update({'label': data.copy()['label']})
        if data.get('id', ''):
            dst_data.update({'id': data.copy()['id']})
        # print_("Raw incoming data: %s" % data)
        for name, prop in cls._properties.items():
            # print_("trying db_field_name: %s and name: %s" % (prop.db_field_name, name))
            if prop.db_field_name in dst_data:
                dst_data[name] = dst_data.pop(prop.db_field_name)
            elif name in dst_data:
                dst_data[name] = dst_data.pop(name)

        return dst_data

    @classmethod
    def create(cls, *args, **kwargs):
        """Create a new element with the given information."""
        # pop the optional execute query arguments from kwargs
        query_kwargs = connection.pop_execute_query_kwargs(kwargs)
        return cls(*args, **kwargs).save(**query_kwargs)

    def pre_save(self):
        """Pre-save hook which is run before saving an element"""
        self.validate()

    def save(self):
        """
        Base class save method. Performs basic validation and error handling.
        """
        if self.__abstract__:
            raise GoblinException('cant save abstract elements')
        self.pre_save()
        return self

    def pre_update(self, **values):
        """ Override this to perform pre-update validation """
        pass

    def update(self, **values):
        """
        performs an update of this element with the given values and returns
        the saved object
        """
        if self.__abstract__:
            raise GoblinException('cant update abstract elements')
        self.pre_update(**values)
        for key in values.keys():
            if key not in self._properties:
                raise TypeError(
                    "unrecognized attribute name: '{}'".format(key))

        for k, v in values.items():
            setattr(self, k, v)

        return self.save()

    def _reload_values(self):
        """
        Base method for reloading an element from the database.

        """
        raise NotImplementedError

    def reload(self, *args, **kwargs):
        """
        Reload the given element from the database.

        """
        future = connection.future_class()
        future_values = self._reload_values(**kwargs)

        def on_reload(f):
            try:
                values = f.result()
            except Exception as e:
                future.set_exception(e)
            else:
                for name, prop in self._properties.items():
                    value = values.get(prop.db_field_name, None)
                    if value is not None:
                        value = prop.to_python(value)
                    setattr(self, name, value)
                future.set_result(self)

        future_values.add_done_callback(on_reload)

        return future

    @classmethod
    def get_property_by_name(cls, key):
        """
        Get's the db_field_name of a property by key

        :param key: attribute of the model
        :type key: basestring | str
        :rtype: basestring | str | None
        """
        if isinstance(key, string_types):
            prop = cls._properties.get(key, None)
            if prop:
                return prop.db_field_name
        return key  # pragma: no cover

    @classmethod
    def _get_factory(cls):
        if getattr(cls, 'FACTORY_CLASS', None):
            factory_cls = getattr(cls, 'FACTORY_CLASS', None)
            if isinstance(factory_cls, string_types):
                factory_cls = import_string(factory_cls)
            create_cls = factory_cls.create
        else:
            create_cls = cls.create

        return create_cls

    def __getitem__(self, item):
        value = self._properties.get(item, None)
        if value is not None:
            # call the normal getattr method
            return getattr(self, item)
        else:
            # manual entry
            value_manager = self._manual_values.get(item, None)
            """ :type value_manager:
                    goblin.properties.base.BaseValueManager | None """
            if value_manager is None:
                raise AttributeError(item)
            return value_manager.value

    def __setitem__(self, key, value):
        prop = self._properties.get(key, None)
        if prop is not None:
            # call the normal setattr method
            setattr(self, key, value)
        else:
            # manual entry
            if key in self._manual_values:
                # manual entry already exists, update
                self._manual_values[key].setval(value)
            else:
                # manual entry doesn't exist, create
                from goblin.properties.base import BaseValueManager
                self._manual_values[key] = BaseValueManager(None, value)

    def __delitem__(self, key):
        prop = self._properties.get(key, None)
        if prop is not None:
            # call the normal delattr method
            delattr(self, key)
        else:
            # manual entry
            if key in self._manual_values:
                # manual entry already exists, update for delete
                self._manual_values[key] = None
            else:
                raise AttributeError(key)

    def __contains__(self, item):
        return item in set(self._properties.keys()).union(
            set(self._manual_values.keys()))

    def __len__(self):
        return len(set(self._properties.keys()).union(
            set(self._manual_values.keys())))

    def __iter__(self):
        for item in set(self._properties.keys()).union(
                set(self._manual_values.keys())):
            yield item

    def items(self):
        items = []
        for key in self._properties.keys():
            items.append((key, getattr(self, key)))
        items.extend([(pair[0], pair[1].value) for pair in
                      self._manual_values.items() if pair[1] is not None])
        return items

    def keys(self):
        items = []
        items.extend(self._properties.keys())
        items.extend([k for k in self._manual_values.keys() if
                      self._manual_values.get(k) is not None])
        return items

    def values(self):
        items = []
        for key in self._properties.keys():
            items.append(getattr(self, key))
        items.extend([v.value for v in self._manual_values.values() if
                      v is not None])
        return items


class ElementMetaClass(type):
    """Metaclass for all graph elements"""

    def __new__(mcs, name, bases, body):
        """
        """
        # move graph property definitions into graph property dict
        # and set default column names
        prop_dict = OrderedDict()
        relationship_dict = OrderedDict()

        # get inherited properties
        for base in bases:
            if body.get('__enum_id_only__', None) is None:
                body['__enum_id_only__'] = getattr(base, '__enum_id_only__',
                                                   True)
            for k, v in getattr(base, '_properties', {}).items():
                prop_dict.setdefault(k, v)
            for k, v in getattr(base, '_relationships', {}).items():
                relationship_dict.setdefault(k, v)

        # print_("Name: %s\n\tBases: %s\n\tBody: %s" % (name, bases, body.keys()))

        def _transform_property(prop_name, prop_obj):
            prop_dict[prop_name] = prop_obj
            prop_obj.set_property_name(prop_name)
            if prop_obj.db_field_prefix is not None:
                db_field_prefix_name = name.lower()
                prop_obj.set_db_field_prefix(db_field_prefix_name)
            # set properties
            _get = lambda self: self._values[prop_name].getval()
            _set = lambda self, val: self._values[prop_name].setval(val)
            _del = lambda self: self._values[prop_name].delval()
            if prop_obj.can_delete:
                body[prop_name] = property(_get, _set, _del)
            else:  # pragma: no cover
                body[prop_name] = property(_get, _set)

        property_definitions = [(k, v) for k, v in body.items() if
                                isinstance(v, properties.GraphProperty)]
        property_definitions = sorted(property_definitions,  # cmp=lambda x, y: cmp(x[1].position, y[1].position),
                                      key=lambda x: x[1].position)

        # TODO: check that the defined graph properties don't conflict with any
        # of the
        # Model API's existing attributes/methods transform column definitions
        for k, v in property_definitions:
            _transform_property(k, v)

        # check for duplicate graph property names
        prop_names = set()
        for v in prop_dict.values():
            if v.db_field_name in prop_names:
                raise ModelException(
                    "%s defines the graph property %s more than once" % (
                        name, v.db_field_name))
            prop_names.add(v.db_field_name)

        # create db_name -> model name map for loading
        db_map = {}
        for field_name, prop in prop_dict.items():
            db_map[prop.db_field_name] = field_name

        # add management members to the class
        body['_properties'] = prop_dict
        body['_db_map'] = db_map

        # Manage relationship attributes
        def wrap_relationship(relationship):
            def relationship_wrapper(self):
                relationship._setup_instantiated_vertex(self)
                return relationship
            return relationship_wrapper

        from goblin.relationships import Relationship
        from goblin.tools import LazyImportClass
        for k, v in body.items():
            if isinstance(v, (Relationship, LazyImportClass)):
                relationship_dict[k] = v
                method = wrap_relationship(v)
                body[k] = property(method)
        body['_relationships'] = relationship_dict

        # auto link gremlin methods
        gremlin_methods = {}

        # get inherited gremlin methods
        for base in bases:
            for k, v in getattr(base, '_gremlin_methods', {}).items():
                gremlin_methods.setdefault(k, v)

        # short circuit __abstract__ inheritance
        body['__abstract__'] = body.get('__abstract__', False)

        # short circuit path inheritance
        gremlin_path = body.get('gremlin_path')
        body['gremlin_path'] = gremlin_path

        def wrap_method(method):
            def method_wrapper(self, *args, **kwargs):
                return method(self, *args, **kwargs)
            return method_wrapper

        for k, v in body.items():
            if isinstance(v, BaseGremlinMethod):
                gremlin_methods[k] = v
                method = wrap_method(v)
                body[k] = method
                if v.classmethod:
                    body[k] = classmethod(method)
                if v.property:
                    body[k] = property(method)

        body['_gremlin_methods'] = gremlin_methods

        # create the class and add a QuerySet to it
        klass = super(ElementMetaClass, mcs).__new__(mcs, name, bases, body)

        # configure the gremlin methods
        for name, method in gremlin_methods.items():
            method.configure_method(klass, name, gremlin_path)

        connection._add_model_to_space(klass)
        return klass


@add_metaclass(ElementMetaClass)
class Element(BaseElement):
    # __metaclass__ = ElementMetaClass

    @classmethod
    def deserialize(cls, data):
        """ Deserializes rexpro response into vertex or edge objects """
        dtype = data.get('type')
        data_id = data.get('id')
        properties = data.get('properties')
        label = data['label']
        if dtype == 'vertex':
            # properties are more complex now, this is a temporary hack for
            # the prototype
            properties = {k: v[0]["value"] for (k, v) in properties.items()}
            data["properties"] = properties
            if label not in vertex_types:
                raise ElementDefinitionException(
                    'Vertex "%s" not defined' % label)

            translated_data = vertex_types[label].translate_db_fields(data)
            v = vertex_types[label](**translated_data)
            return v

        elif dtype == 'edge':
            if label not in edge_types:
                raise ElementDefinitionException(
                    'Edge "%s" not defined' % label)

            translated_data = edge_types[label].translate_db_fields(data)
            return edge_types[label](data['outV'], data['inV'],
                                     **translated_data)

        else:
            raise TypeError("Can't deserialize '%s'" % dtype)
