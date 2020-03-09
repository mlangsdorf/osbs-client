"""
Copyright (c) 2018, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, absolute_import, unicode_literals

import abc
import logging
import os
import json

import six

from osbs.constants import BUILD_TYPE_ORCHESTRATOR
from osbs.exceptions import OsbsException

logger = logging.getLogger(__name__)


class PluginsTemplate(object):
    def __init__(self, build_json_dir, template_path, customize_conf_path=None):
        self._template = None
        self._customize_conf = None
        self._build_json_dir = build_json_dir
        self._template_path = template_path
        self._customize_conf_path = customize_conf_path

    @property
    def template(self):
        if self._template is None:
            path = os.path.join(self._build_json_dir, self._template_path)
            logger.debug("loading template from path %s", path)
            try:
                with open(path, "r") as fp:
                    self._template = json.load(fp)
            except (IOError, OSError) as ex:
                raise OsbsException("Can't open template '%s': %s" %
                                    (path, repr(ex)))
        return self._template

    @property
    def customize_conf(self):
        if self._customize_conf is None:
            if self._customize_conf_path is None:
                self._customize_conf = {}
            else:
                path = os.path.join(self._build_json_dir, self._customize_conf_path)
                logger.info('loading customize conf from path %s', path)
                try:
                    with open(path, "r") as fp:
                        self._customize_conf = json.load(fp)
                except IOError:
                    # File not found, which is perfectly fine. Set to empty dict
                    logger.info('failed to find customize conf from path %s', path)
                    self._customize_conf = {}
        return self._customize_conf

    def remove_plugin(self, phase, name, reason=None):
        """
        if config contains plugin, remove it
        """
        for p in self.template[phase]:
            if p.get('name') == name:
                self.template[phase].remove(p)
                if reason:
                    logger.info('Removing %s:%s, %s', phase, name, reason)
                break

    def add_plugin(self, phase, name, args, reason=None):
        """
        if config has plugin, override it, else add it
        """
        plugin_modified = False

        for plugin in self.template[phase]:
            if plugin['name'] == name:
                plugin['args'] = args
                plugin_modified = True

        if not plugin_modified:
            self.template[phase].append({"name": name, "args": args})
            if reason:
                logger.info('%s:%s with args %s, %s', phase, name, args, reason)

    def get_plugin_conf(self, phase, name):
        """
        Return the configuration for a plugin.

        Raises KeyError if there are no plugins of that type.
        Raises IndexError if the named plugin is not listed.
        """
        match = [x for x in self.template[phase] if x.get('name') == name]
        return match[0]

    def has_plugin_conf(self, phase, name):
        """
        Check whether a plugin is configured.
        """
        try:
            self.get_plugin_conf(phase, name)
            return True
        except (KeyError, IndexError):
            return False

    def _get_plugin_conf_or_fail(self, phase, name):
        try:
            conf = self.get_plugin_conf(phase, name)
        except KeyError:
            raise RuntimeError("Invalid template: plugin phase '%s' misses" % phase)
        except IndexError:
            raise RuntimeError("no such plugin in template: \"%s\"" % name)
        return conf

    def set_plugin_arg(self, phase, name, arg_key, arg_value):
        plugin_conf = self._get_plugin_conf_or_fail(phase, name)
        plugin_conf.setdefault("args", {})
        plugin_conf['args'][arg_key] = arg_value

    def set_plugin_arg_valid(self, phase, plugin, name, value):
        if value is not None:
            self.set_plugin_arg(phase, plugin, name, value)
            return True
        return False

    def to_json(self):
        return json.dumps(self.template)


@six.add_metaclass(abc.ABCMeta)
class PluginsConfigurationBase(object):
    """Abstract class for Plugins Configuration

    Following properties must be implemented:
      * pt_path - path to inner config

    Following methods must be implemented:
      * render - method generates plugin config JSON

    Class contains methods that configures plugins. These methods should be
    used only if needed in specific subclass implementation
    """
    def __init__(self, user_params):
        self.user_params = user_params

        customize_conf_path = (
            self.user_params.customize_conf_path.value
            if hasattr(self.user_params, 'customize_conf_path')
            else None
        )

        self.pt = PluginsTemplate(
            self.user_params.build_json_dir.value,
            self.pt_path,
            customize_conf_path,
        )

    @abc.abstractproperty
    def pt_path(self):
        """Property returns path to plugins template JSON file

        :return: file path
        """
        raise NotImplementedError

    @abc.abstractmethod
    def render(self):
        """Return plugins configuration JSON

        :rval: str
        :return: JSON
        """
        raise NotImplementedError

    def has_tag_suffixes_placeholder(self):
        phase = 'postbuild_plugins'
        plugin = 'tag_from_config'
        if not self.pt.has_plugin_conf(phase, plugin):
            logger.debug('no tag suffix placeholder')
            return False

        placeholder = '{{TAG_SUFFIXES}}'
        plugin_conf = self.pt.get_plugin_conf('postbuild_plugins', 'tag_from_config')
        return plugin_conf.get('args', {}).get('tag_suffixes') == placeholder

    def adjust_for_scratch(self):
        """
        Remove certain plugins in order to handle the "scratch build"
        scenario. Scratch builds must not affect subsequent builds,
        and should not be imported into Koji.
        """
        if self.user_params.scratch.value:
            remove_plugins = [
                ("prebuild_plugins", "koji_parent"),
                ("postbuild_plugins", "compress"),  # required only to make an archive for Koji
                ("postbuild_plugins", "compare_components"),
                ("postbuild_plugins", "import_image"),
                ("exit_plugins", "koji_tag_build"),
                ("exit_plugins", "import_image"),
                ("prebuild_plugins", "check_and_set_rebuild"),
                ("prebuild_plugins", "stop_autorebuild_if_disabled")
            ]

            if not self.has_tag_suffixes_placeholder():
                remove_plugins.append(("postbuild_plugins", "tag_from_config"))

            for when, which in remove_plugins:
                self.pt.remove_plugin(when, which, 'removed from scratch build request')

    def adjust_for_isolated(self):
        """
        Remove certain plugins in order to handle the "isolated build"
        scenario.
        """
        if self.user_params.isolated.value:
            remove_plugins = [
                ("prebuild_plugins", "check_and_set_rebuild"),
                ("prebuild_plugins", "stop_autorebuild_if_disabled")
            ]

            for when, which in remove_plugins:
                self.pt.remove_plugin(when, which, 'removed from isolated build request')

    def adjust_for_flatpak(self):
        """
        Remove plugins that don't work when building Flatpaks
        """
        if self.user_params.flatpak.value:
            remove_plugins = [
                # We'll extract the filesystem anyways for a Flatpak instead of exporting
                # the docker image directly, so squash just slows things down.
                ("prepublish_plugins", "squash"),
            ]
            for when, which in remove_plugins:
                self.pt.remove_plugin(when, which, 'not needed for flatpak build')

    def render_add_filesystem(self):
        phase = 'prebuild_plugins'
        plugin = 'add_filesystem'

        if self.pt.has_plugin_conf(phase, plugin):
            self.pt.set_plugin_arg_valid(phase, plugin, 'repos',
                                         self.user_params.yum_repourls.value)
            self.pt.set_plugin_arg_valid(phase, plugin, 'from_task_id',
                                         self.user_params.filesystem_koji_task_id.value)
            self.pt.set_plugin_arg_valid(phase, plugin, 'architecture',
                                         self.user_params.platform.value)
            self.pt.set_plugin_arg_valid(phase, plugin, 'koji_target',
                                         self.user_params.koji_target.value)

    def render_add_flatpak_labels(self):
        phase = 'prebuild_plugins'
        plugin = 'add_flatpak_labels'

        if self.pt.has_plugin_conf(phase, plugin):
            if not self.user_params.flatpak.value:
                self.pt.remove_plugin(phase, plugin)
                return

    def render_add_labels_in_dockerfile(self):
        phase = 'prebuild_plugins'
        plugin = 'add_labels_in_dockerfile'
        if self.pt.has_plugin_conf(phase, plugin):
            if self.user_params.release.value:
                release_label = {'release': self.user_params.release.value}
                self.pt.set_plugin_arg(phase, plugin, 'labels', release_label)

    def render_add_yum_repo_by_url(self):
        if self.pt.has_plugin_conf('prebuild_plugins', "add_yum_repo_by_url"):
            self.pt.set_plugin_arg_valid('prebuild_plugins', "add_yum_repo_by_url", "repourls",
                                         self.user_params.yum_repourls.value)

    def render_customizations(self):
        """
        Customize template for site user specified customizations
        """
        disable_plugins = self.pt.customize_conf.get('disable_plugins', [])
        if not disable_plugins:
            logger.debug('No site-user specified plugins to disable')
        else:
            for plugin in disable_plugins:
                try:
                    self.pt.remove_plugin(plugin['plugin_type'], plugin['plugin_name'],
                                          'disabled at user request')
                except KeyError:
                    # Malformed config
                    logger.info('Invalid custom configuration found for disable_plugins')

        enable_plugins = self.pt.customize_conf.get('enable_plugins', [])
        if not enable_plugins:
            logger.debug('No site-user specified plugins to enable')
        else:
            for plugin in enable_plugins:
                try:
                    msg = 'enabled at user request'
                    self.pt.add_plugin(plugin['plugin_type'], plugin['plugin_name'],
                                       plugin['plugin_args'], msg)
                except KeyError:
                    # Malformed config
                    logger.info('Invalid custom configuration found for enable_plugins')

    def render_check_user_settings(self):
        phase = 'prebuild_plugins'
        plugin = 'check_user_settings'
        if self.pt.has_plugin_conf(phase, plugin):
            self.pt.set_plugin_arg_valid(phase, plugin, 'flatpak',
                                         self.user_params.flatpak.value)

    def render_flatpak_create_dockerfile(self):
        phase = 'prebuild_plugins'
        plugin = 'flatpak_create_dockerfile'

        if self.pt.has_plugin_conf(phase, plugin):

            if not self.user_params.flatpak.value:
                self.pt.remove_plugin(phase, plugin)
                return

    def render_flatpak_create_oci(self):
        phase = 'prepublish_plugins'
        plugin = 'flatpak_create_oci'

        if not self.user_params.flatpak.value:
            self.pt.remove_plugin(phase, plugin)

    def render_flatpak_update_dockerfile(self):
        phase = 'prebuild_plugins'
        plugin = 'flatpak_update_dockerfile'

        if self.pt.has_plugin_conf(phase, plugin):

            if not self.user_params.flatpak.value:
                self.pt.remove_plugin(phase, plugin)
                return

            self.pt.set_plugin_arg_valid(phase, plugin, 'compose_ids',
                                         self.user_params.compose_ids.value)

    def render_koji(self):
        """
        if there is yum repo in user params, don't pick stuff from koji
        """
        phase = 'prebuild_plugins'
        plugin = 'koji'
        if not self.pt.has_plugin_conf(phase, plugin):
            return

        if self.user_params.yum_repourls.value:
            self.pt.remove_plugin(phase, plugin, 'there is a yum repo user parameter')
        elif not self.pt.set_plugin_arg_valid(phase, plugin, "target",
                                              self.user_params.koji_target.value):
            self.pt.remove_plugin(phase, plugin, 'no koji target supplied in user parameters')

    def render_bump_release(self):
        """
        If the bump_release plugin is present, configure it
        """
        phase = 'prebuild_plugins'
        plugin = 'bump_release'
        if not self.pt.has_plugin_conf(phase, plugin):
            return

        if self.user_params.release.value:
            self.pt.remove_plugin(phase, plugin, 'release value supplied as user parameter')
            return

        # For flatpak, we want a name-version-release of
        # <name>-<stream>-<module_build_version>.<n>, where the .<n> makes
        # sure that the build is unique in Koji
        if self.user_params.flatpak.value:
            self.pt.set_plugin_arg(phase, plugin, 'append', True)

    def render_check_and_set_platforms(self):
        """
        If the check_and_set_platforms plugin is present, configure it
        """
        phase = 'prebuild_plugins'
        plugin = 'check_and_set_platforms'
        if not self.pt.has_plugin_conf(phase, plugin):
            return

        if self.user_params.koji_target.value:
            self.pt.set_plugin_arg(phase, plugin, "koji_target",
                                   self.user_params.koji_target.value)

    def render_import_image(self, use_auth=None):
        """
        Configure the import_image plugin
        """
        # import_image is a multi-phase plugin
        if self.user_params.imagestream_name.value is None:
            self.pt.remove_plugin('exit_plugins', 'import_image',
                                  'imagestream not in user parameters')
        elif self.pt.has_plugin_conf('exit_plugins', 'import_image'):
            self.pt.set_plugin_arg('exit_plugins', 'import_image', 'imagestream',
                                   self.user_params.imagestream_name.value)

    def render_inject_parent_image(self):
        phase = 'prebuild_plugins'
        plugin = 'inject_parent_image'
        if not self.pt.has_plugin_conf(phase, plugin):
            return

        koji_parent_build = self.user_params.koji_parent_build.value

        if not koji_parent_build:
            self.pt.remove_plugin(phase, plugin, 'no koji parent build in user parameters')
            return

        self.pt.set_plugin_arg(phase, plugin, 'koji_parent_build', koji_parent_build)

    def render_koji_upload(self, use_auth=None):
        phase = 'postbuild_plugins'
        name = 'koji_upload'
        if not self.pt.has_plugin_conf(phase, name):
            return

        def set_arg(arg, value):
            self.pt.set_plugin_arg(phase, name, arg, value)

        set_arg('koji_upload_dir', self.user_params.koji_upload_dir.value)
        set_arg('platform', self.user_params.platform.value)
        set_arg('report_multiple_digests', True)

    def render_export_operator_manifests(self):
        phase = 'postbuild_plugins'
        name = 'export_operator_manifests'
        if not self.pt.has_plugin_conf(phase, name):
            return

        self.pt.set_plugin_arg(phase, name, 'platform', self.user_params.platform.value)
        if self.user_params.operator_manifests_extract_platform.value:
            self.pt.set_plugin_arg(phase, name, 'operator_manifests_extract_platform',
                                   self.user_params.operator_manifests_extract_platform.value)

    def render_koji_tag_build(self):
        phase = 'exit_plugins'
        plugin = 'koji_tag_build'
        if not self.pt.has_plugin_conf(phase, plugin):
            return

        if not self.user_params.koji_target.value:
            self.pt.remove_plugin(phase, plugin, 'no koji target in user parameters')
            return

        self.pt.set_plugin_arg(phase, plugin, 'target', self.user_params.koji_target.value)

    def render_orchestrate_build(self):
        phase = 'buildstep_plugins'
        plugin = 'orchestrate_build'
        if not self.pt.has_plugin_conf(phase, plugin):
            return

        # Parameters to be used in call to create_worker_build
        worker_params = [
            'component', 'git_branch', 'git_ref', 'git_uri', 'koji_task_id',
            'filesystem_koji_task_id', 'scratch', 'koji_target', 'user', 'yum_repourls',
            'arrangement_version', 'koji_parent_build', 'isolated', 'reactor_config_map',
            'reactor_config_override', 'git_commit_depth',
        ]

        build_kwargs = self.user_params.to_dict(worker_params)
        # koji_target is passed as target for some reason
        build_kwargs['target'] = build_kwargs.pop('koji_target', None)

        if self.user_params.flatpak.value:
            build_kwargs['flatpak'] = True

        self.pt.set_plugin_arg_valid(phase, plugin, 'platforms', self.user_params.platforms.value)
        self.pt.set_plugin_arg(phase, plugin, 'build_kwargs', build_kwargs)

        config_kwargs = {}

        if not self.user_params.build_imagestream.value:
            config_kwargs['build_from'] = 'image:' + self.user_params.build_image.value

        self.pt.set_plugin_arg(phase, plugin, 'config_kwargs', config_kwargs)

    def render_resolve_composes(self):
        phase = 'prebuild_plugins'
        plugin = 'resolve_composes'

        if not self.pt.has_plugin_conf(phase, plugin):
            return

        self.pt.set_plugin_arg_valid(phase, plugin, 'compose_ids',
                                     self.user_params.compose_ids.value)

        self.pt.set_plugin_arg_valid(phase, plugin, 'signing_intent',
                                     self.user_params.signing_intent.value)

        self.pt.set_plugin_arg_valid(phase, plugin, 'koji_target',
                                     self.user_params.koji_target.value)

        self.pt.set_plugin_arg_valid(phase, plugin, 'repourls',
                                     self.user_params.yum_repourls.value)

    def render_tag_from_config(self):
        """Configure tag_from_config plugin"""
        phase = 'postbuild_plugins'
        plugin = 'tag_from_config'
        if not self.has_tag_suffixes_placeholder():
            return

        unique_tag = self.user_params.image_tag.value.split(':')[-1]
        tag_suffixes = {'unique': [unique_tag], 'primary': [], 'floating': []}

        if self.user_params.build_type.value == BUILD_TYPE_ORCHESTRATOR:
            additional_tags = self.user_params.additional_tags.value or set()

            if self.user_params.scratch.value:
                pass
            elif self.user_params.isolated.value:
                tag_suffixes['primary'].extend(['{version}-{release}'])
            elif self.user_params.tags_from_yaml.value:
                tag_suffixes['primary'].extend(['{version}-{release}'])
                tag_suffixes['floating'].extend(additional_tags)
            else:
                tag_suffixes['primary'].extend(['{version}-{release}'])
                tag_suffixes['floating'].extend(['latest', '{version}'])
                tag_suffixes['floating'].extend(additional_tags)

        self.pt.set_plugin_arg(phase, plugin, 'tag_suffixes', tag_suffixes)

    def render_pull_base_image(self):
        """Configure pull_base_image"""
        phase = 'prebuild_plugins'
        plugin = 'pull_base_image'

        if self.user_params.parent_images_digests.value:
            self.pt.set_plugin_arg(phase, plugin, 'parent_images_digests',
                                   self.user_params.parent_images_digests.value)

    def render_koji_delegate(self):
        """Configure koji_delegate"""
        phase = 'prebuild_plugins'
        plugin = 'koji_delegate'

        if self.pt.has_plugin_conf(phase, plugin):
            if self.user_params.triggered_after_koji_task.value:
                self.pt.set_plugin_arg(phase, plugin, 'triggered_after_koji_task',
                                       self.user_params.triggered_after_koji_task.value)

    def render_tag_and_push(self):
        """Configure tag_and_push plugin"""
        phase = 'postbuild_plugins'
        plugin = 'tag_and_push'

        if self.pt.has_plugin_conf(phase, plugin):
            if self.user_params.koji_target.value:
                self.pt.set_plugin_arg(
                    phase, plugin,
                    'koji_target',
                    self.user_params.koji_target.value
                )

    def render_fetch_sources(self):
        """Configure fetch_sources"""
        phase = 'prebuild_plugins'
        plugin = 'fetch_sources'

        if self.pt.has_plugin_conf(phase, plugin):
            if self.user_params.sources_for_koji_build_nvr.value:
                self.pt.set_plugin_arg(
                    phase, plugin,
                    'koji_build_nvr',
                    self.user_params.sources_for_koji_build_nvr.value
                )

            if self.user_params.sources_for_koji_build_id.value:
                self.pt.set_plugin_arg(
                    phase, plugin,
                    'koji_build_id',
                    self.user_params.sources_for_koji_build_id.value
                )

            if self.user_params.signing_intent.value:
                self.pt.set_plugin_arg(
                    phase, plugin,
                    'signing_intent',
                    self.user_params.signing_intent.value
                )

    def render_download_remote_source(self):
        phase = 'prebuild_plugins'
        plugin = 'download_remote_source'

        if self.pt.has_plugin_conf(phase, plugin):
            self.pt.set_plugin_arg(phase, plugin, 'remote_source_url',
                                   self.user_params.remote_source_url.value)
            self.pt.set_plugin_arg(phase, plugin, 'remote_source_build_args',
                                   self.user_params.remote_source_build_args.value)

    def render_resolve_remote_source(self):
        phase = 'prebuild_plugins'
        plugin = 'resolve_remote_source'

        if self.pt.has_plugin_conf(phase, plugin):
            self.pt.set_plugin_arg_valid(phase, plugin, "dependency_replacements",
                                         self.user_params.dependency_replacements.value)


class PluginsConfiguration(PluginsConfigurationBase):
    """Plugin configuration for image builds"""

    @property
    def pt_path(self):
        arrangement_version = self.user_params.arrangement_version.value
        build_type = self.user_params.build_type.value
        #    <build_type>_inner:<arrangement_version>.json
        return '{}_inner:{}.json'.format(build_type, arrangement_version)

    def render(self):
        self.user_params.validate()
        # adjust for custom configuration first
        self.render_customizations()

        self.adjust_for_scratch()
        self.adjust_for_isolated()
        self.adjust_for_flatpak()

        # Set parameters on each plugin as needed
        self.render_add_filesystem()
        self.render_add_flatpak_labels()
        self.render_add_labels_in_dockerfile()
        self.render_add_yum_repo_by_url()
        self.render_bump_release()
        self.render_check_and_set_platforms()
        self.render_check_user_settings()
        self.render_flatpak_create_dockerfile()
        self.render_flatpak_create_oci()
        self.render_flatpak_update_dockerfile()
        self.render_import_image()
        self.render_inject_parent_image()
        self.render_koji()
        self.render_koji_tag_build()
        self.render_koji_upload()
        self.render_export_operator_manifests()
        self.render_orchestrate_build()
        self.render_pull_base_image()
        self.render_resolve_composes()
        self.render_tag_from_config()
        self.render_koji_delegate()
        self.render_download_remote_source()
        self.render_resolve_remote_source()
        return self.pt.to_json()


class SourceContainerPluginsConfiguration(PluginsConfigurationBase):
    """Plugins configuration for source container image builds"""

    @property
    def pt_path(self):
        arrangement_version = self.user_params.arrangement_version.value
        # orchestrator_sources_inner:<arrangement_version>.json
        return 'orchestrator_sources_inner:{}.json'.format(arrangement_version)

    def adjust_for_scratch(self):
        """
        Remove certain plugins in order to handle the "scratch build"
        scenario. Scratch builds must not affect subsequent builds,
        and should not be imported into Koji.
        """
        if self.user_params.scratch.value:
            remove_plugins = [
                ("postbuild_plugins", "compress"),  # required only to make an archive for Koji
                ("exit_plugins", "koji_tag_build"),
            ]

            for when, which in remove_plugins:
                self.pt.remove_plugin(when, which, 'removed from scratch build request')

    def render(self):
        self.user_params.validate()
        # adjust for custom configuration first
        self.render_customizations()

        self.adjust_for_scratch()
        # Set parameters on each plugin as needed
        # self.render_bump_release()  # not needed yet
        self.render_fetch_sources()
        self.render_koji()
        self.render_koji_tag_build()
        self.render_tag_and_push()

        return self.pt.to_json()
