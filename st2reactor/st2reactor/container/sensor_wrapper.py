# Licensed to the StackStorm, Inc ('StackStorm') under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import json
import atexit
import argparse

from oslo.config import cfg

from st2common import log as logging
from st2common.models.db import db_setup
from st2common.transport.reactor import TriggerDispatcher
from st2common.util import loader
from st2common.util.config_parser import ContentPackConfigParser
from st2common.services.triggerwatcher import TriggerWatcher
from st2reactor.sensor.base import Sensor
from st2reactor.sensor import config
from st2common.constants.pack import SYSTEM_PACK_NAMES

__all__ = [
    'SensorWrapper'
]


class SensorService(object):
    """
    Instance of this class is passed to the sensor instance and exposes "public"
    methods which can be called by the sensor.
    """

    def __init__(self, sensor_wrapper):
        self._sensor_wrapper = sensor_wrapper
        self._logger = self._sensor_wrapper._logger
        self._dispatcher = TriggerDispatcher(self._logger)

    def get_logger(self, name):
        """
        Retrieve an instance of a logger to be used by the sensor class.
        """
        logger_name = '%s.%s' % (self._sensor_wrapper._logger.name, name)
        logger = logging.getLogger(logger_name)
        logger.propagate = True
        return logger

    def dispatch(self, trigger, payload=None):
        """
        Method which dispatches the trigger.

        :param trigger: Full name / reference of the trigger.
        :type trigger: ``str``

        :param payload: Trigger payload.
        :type payload: ``dict``
        """
        self._dispatcher.dispatch(trigger, payload=payload)

    def get_value(self, name):
        """
        Retrieve a value from the datastore for the provided key.

        :param name: Key name.
        :type name: ``str``
        """
        name = self._get_full_key_name(name=name)
        pass

    def set_value(self, name, value):
        """
        Set a value for the provided key.

        :param name: Key name.
        :type name: ``str``

        :param value: Key value.
        :type value: ``str``
        """
        name = self._get_full_key_name(name=name)
        pass

    def delete_value(self, name):
        """
        Delete the provided key.

        :param name: Name of the key to delete.
        :type name: ``str``
        """
        name = self._get_full_key_name(name=name)
        pass

    def _get_full_key_name(self, name):
        prefix = self._get_datastore_key_prefix()
        separator = '.'
        full_name = prefix + separator + name
        return full_name

    def _get_datastore_key_prefix(self):
        prefix = '%s.%s' % (self._sensor_wrapper._pack, self._sensor_wrapper.class_name)
        return prefix


class SensorWrapper(object):
    def __init__(self, pack, file_path, class_name, trigger_types,
                 poll_interval=None, parent_args=None):
        """
        :param pack: Name of the pack this sensor belongs to.
        :type pack: ``str``

        :param file_path: Path to the sensor module file.
        :type file_path: ``str``

        :param class_name: Sensor class name.
        :type class_name: ``str``

        :param trigger_types: A list of references to trigger types which
                                  belong to this sensor.
        :type trigger_types: ``list`` of ``str``

        :param poll_interval: Sensor poll interval (in seconds).
        :type poll_interval: ``int`` or ``None``

        :param parent_args: Command line arguments passed to the parent process.
        :type parse_args: ``list``
        """
        self._pack = pack
        self._file_path = file_path
        self._class_name = class_name
        self._trigger_types = trigger_types or []
        self._poll_interval = poll_interval
        self._parent_args = parent_args or []
        self._trigger_names = {}

        # 1. Parse the config with inherited parent args
        try:
            config.parse_args(args=self._parent_args)
        except Exception:
            pass

        # 2. Establish DB connection
        username = cfg.CONF.database.username if hasattr(cfg.CONF.database, 'username') else None
        password = cfg.CONF.database.password if hasattr(cfg.CONF.database, 'password') else None
        db_setup(cfg.CONF.database.db_name, cfg.CONF.database.host, cfg.CONF.database.port,
                 username=username, password=password)

        # 3. Instantiate the watcher
        self._trigger_watcher = TriggerWatcher(create_handler=self._handle_create_trigger,
                                               update_handler=self._handle_update_trigger,
                                               delete_handler=self._handle_delete_trigger,
                                               trigger_types=self._trigger_types,
                                               queue_suffix='sensorwrapper')

        # 4. Set up logging
        self._logger = logging.getLogger('SensorWrapper.%s' %
                                         (self._class_name))
        logging.setup(cfg.CONF.sensorcontainer.logging)

        self._sensor_instance = self._get_sensor_instance()

    def run(self):
        atexit.register(self.stop)

        self._trigger_watcher.start()
        self._logger.info('Watcher started')

        self._logger.info('Running sensor initialization code')
        self._sensor_instance.setup()

        if self._poll_interval:
            message = ('Running sensor in active mode (poll interval=%ss)' %
                       (self._poll_interval))
        else:
            message = 'Running sensor in passive mode'

        self._logger.info(message)

        try:
            self._sensor_instance.run()
        except Exception as e:
            raise Exception('Sensor "%s" run method raised an exception: %s' %
                            (self._class_name, str(e)))

    def stop(self):
        # Stop watcher
        self._logger.info('Stopping trigger watcher')
        self._trigger_watcher.stop()

        # Run sensor cleanup code
        self._logger.info('Invoking cleanup on sensor')
        self._sensor_instance.cleanup()

    ##############################################
    # Event handler methods for the trigger events
    ##############################################

    def _handle_create_trigger(self, trigger):
        self._logger.debug('Calling sensor "add_trigger" method (trigger.type=%s)' %
                           (trigger.type))
        self._trigger_names[str(trigger.id)] = trigger

        trigger = self._sanitize_trigger(trigger=trigger)
        self._sensor_instance.add_trigger(trigger=trigger)

    def _handle_update_trigger(self, trigger):
        self._logger.debug('Calling sensor "update_trigger" method (trigger.type=%s)' %
                           (trigger.type))
        self._trigger_names[str(trigger.id)] = trigger

        trigger = self._sanitize_trigger(trigger=trigger)
        self._sensor_instance.update_trigger(trigger=trigger)

    def _handle_delete_trigger(self, trigger):
        trigger_id = str(trigger.id)
        if trigger_id not in self._trigger_names:
            return

        self._logger.debug('Calling sensor "remove_trigger" method (trigger.type=%s)' %
                           (trigger.type))
        del self._trigger_names[trigger_id]

        trigger = self._sanitize_trigger(trigger=trigger)
        self._sensor_instance.remove_trigger(trigger=trigger)

    def _get_sensor_instance(self):
        """
        Retrieve instance of a sensor class.
        """
        _, filename = os.path.split(self._file_path)
        module_name, _ = os.path.splitext(filename)

        sensor_class = loader.register_plugin_class(base_class=Sensor,
                                                    file_path=self._file_path,
                                                    class_name=self._class_name)

        if not sensor_class:
            raise ValueError('Sensor module is missing a class with name "%s"' %
                             (self._class_name))

        sensor_class_kwargs = {}
        sensor_class_kwargs['sensor_service'] = SensorService(sensor_wrapper=self)

        sensor_config = self._get_sensor_config()

        if self._pack not in SYSTEM_PACK_NAMES:
            sensor_class_kwargs['config'] = sensor_config

        if self._poll_interval:
            sensor_class_kwargs['poll_interval'] = self._poll_interval

        try:
            sensor_instance = sensor_class(**sensor_class_kwargs)
        except Exception as e:
            raise Exception('Failed to instantiate "%s" sensor class: %s' %
                            (self._class_name, str(e)))

        return sensor_instance

    def _get_sensor_config(self):
        config_parser = ContentPackConfigParser(pack_name=self._pack)
        config = config_parser.get_sensor_config(sensor_file_path=self._file_path)

        if config:
            self._logger.info('Using config "%s" for sensor "%s"' % (config.file_path,
                                                                     self._class_name))
            return config.config
        else:
            self._logger.info('No config found for sensor "%s"' % (self._class_name))
            return {}

    def _sanitize_trigger(self, trigger):
        sanitized = trigger._data
        if 'id' in sanitized:
            # Friendly objectid rather than the MongoEngine representation.
            sanitized['id'] = str(sanitized['id'])
        return sanitized


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sensor runner wrapper')
    parser.add_argument('--pack', required=True,
                        help='Name of the pack this sensor belongs to')
    parser.add_argument('--file-path', required=True,
                        help='Path to the sensor module')
    parser.add_argument('--class-name', required=True,
                        help='Name of the sensor class')
    parser.add_argument('--trigger-type-refs', required=False,
                        help='Comma delimited string of trigger type references')
    parser.add_argument('--poll-interval', type=int, default=None, required=False,
                        help='Sensor poll interval')
    parser.add_argument('--parent-args', required=False,
                        help='Command line arguments passed to the parent process')
    args = parser.parse_args()

    trigger_types = args.trigger_type_refs
    trigger_types = trigger_types.split(',') if trigger_types else []
    parent_args = json.loads(args.parent_args) if args.parent_args else []
    assert isinstance(parent_args, list)

    obj = SensorWrapper(pack=args.pack,
                        file_path=args.file_path,
                        class_name=args.class_name,
                        trigger_types=trigger_types,
                        poll_interval=args.poll_interval,
                        parent_args=parent_args)
    obj.run()
