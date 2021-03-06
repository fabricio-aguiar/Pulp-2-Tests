# coding=utf-8
"""Test cases that copy content units."""
import os
import time
import unittest
from urllib.parse import urljoin

import pytest
from packaging.version import Version
from pulp_smash import api, cli, config, selectors, utils
from pulp_smash.pulp2.constants import REPOSITORY_PATH, ORPHANS_PATH
from pulp_smash.pulp2.utils import (
    publish_repo,
    search_units,
    sync_repo,
    upload_import_unit,
)

from pulp_2_tests.constants import (
    MODULE_FIXTURES_ERRATA,
    MODULE_FIXTURES_PACKAGE_STREAM,
    RPM_NAMESPACES,
    RPM_PACKAGES_MULTIPLE_REPOS,
    RPM_SIGNED_URL,
    RPM_UNSIGNED_FEED_URL,
    RPM_UNSIGNED_MODIFIED_FEED_URL,
    RPM_UPDATED_INFO_FEED_URL,
    RPM_WITH_MODULES_MODIFIED_FEED_URL,
    RPM_WITH_OLD_VERSION_URL,
    RPM_YUM_METADATA_FILE,
)
from pulp_2_tests.tests.rpm.api_v2.utils import (
    gen_distributor,
    gen_repo,
    get_repodata_repomd_xml,
)
from pulp_2_tests.tests.rpm.utils import set_up_module as setUpModule  # pylint:disable=unused-import

_PATH = '/var/lib/pulp/published/yum/https/repos/'


@pytest.mark.recursive_conservative
class CopyErrataRecursiveTestCase(unittest.TestCase):
    """Test that recursive copy of erratas copies RPM packages."""

    def test_all(self):
        """Test that recursive copy of erratas copies RPM packages.

        This test targets the following issues:

        * `Pulp Smash #769 <https://github.com/PulpQE/pulp-smash/issues/769>`_
        * `Pulp #3004 <https://pulp.plan.io/issues/3004>`_

        Do the following:

        1. Create and sync a repository with errata, and RPM packages.
        2. Create second repository.
        3. Copy units from from first repository to second repository
           using ``recursive`` as true, and filter  ``type_id`` as
           ``erratum``.
        4. Assert that RPM packages were copied.
        """
        cfg = config.get_config()
        if not selectors.bug_is_fixed(3004, cfg.pulp_version):
            self.skipTest('https://pulp.plan.io/issues/3004')

        repos = []
        client = api.Client(cfg, api.json_handler)
        body = gen_repo()
        body['importer_config']['feed'] = RPM_UPDATED_INFO_FEED_URL
        body['distributors'] = [gen_distributor()]
        repos.append(client.post(REPOSITORY_PATH, body))
        self.addCleanup(client.delete, repos[0]['_href'])
        sync_repo(cfg, repos[0])

        # Create a second repository.
        repos.append(client.post(REPOSITORY_PATH, gen_repo()))
        self.addCleanup(client.delete, repos[1]['_href'])

        # Copy data to second repository.
        client.post(
            urljoin(repos[1]['_href'], 'actions/associate/'),
            {
                'source_repo_id': repos[0]['id'],
                'override_config': {'recursive': True},
                'criteria': {'filters': {}, 'type_ids': ['erratum']},
            },
        )

        # Assert that RPM packages were copied.
        units = search_units(cfg, repos[1], {'type_ids': ['rpm']})
        self.assertGreater(len(units), 0)


class MtimeTestCase(unittest.TestCase):
    """Test whether copied files retain their original mtime."""

    def test_all(self):
        """Test whether copied files retain their original mtime.

        This test targets the following issues:

        * `Pulp #2783 <https://pulp.plan.io/issues/2783>`_
        * `Pulp Smash #720 <https://github.com/PulpQE/pulp-smash/issues/720>`_

        Do the following:

        1. Create, sync and publish a repository, with ``generate_sqlite`` set
           to true.
        2. Get the ``mtime`` of the sqlite files.
        3. Upload an RPM package into the repository, and sync the repository.
        4. Get the ``mtime`` of the sqlite files again. Verify that the mtimes
           are the same.
        """
        cfg = config.get_config()
        if not selectors.bug_is_fixed(2783, cfg.pulp_version):
            self.skipTest('https://pulp.plan.io/issues/2783')

        # Create, sync and publish a repository.
        client = api.Client(cfg, api.json_handler)
        body = gen_repo()
        body['importer_config']['feed'] = RPM_UNSIGNED_FEED_URL
        body['distributors'] = [gen_distributor()]
        body['distributors'][0]['distributor_config']['generate_sqlite'] = True
        repo = client.post(REPOSITORY_PATH, body)
        self.addCleanup(client.delete, repo['_href'])
        repo = client.get(repo['_href'], params={'details': True})
        sync_repo(cfg, repo)
        publish_repo(cfg, repo)

        # Get the mtime of the sqlite files.
        cli_client = cli.Client(cfg, cli.echo_handler)
        cmd = '' if cli.is_root(cfg) else 'sudo '
        cmd += "bash -c \"stat --format %Y '{}'/*\"".format(
            os.path.join(
                _PATH, repo['distributors'][0]['config']['relative_url'], 'repodata'
            )
        )
        # machine.session is used here to keep SSH session open
        mtimes_pre = cli_client.machine.session().run(cmd)[1].strip().split().sort()

        # Upload to the repo, and sync it.
        rpm = utils.http_get(RPM_SIGNED_URL)
        upload_import_unit(cfg, rpm, {'unit_type_id': 'rpm'}, repo)
        sync_repo(cfg, repo)

        # Get the mtime of the sqlite files again.
        time.sleep(1)
        # machine.session is used here to keep SSH session open
        mtimes_post = cli_client.machine.session().run(cmd)[1].strip().split().sort()
        self.assertEqual(mtimes_pre, mtimes_post)


@pytest.mark.recursive_conservative
class CopyYumMetadataFileTestCase(unittest.TestCase):
    """Test the copy of metadata units between repos."""

    def test_all(self):
        """Test whether metadata copied between repos are independent.

        This test targets the following issues:

        * `Pulp #1944 <https://pulp.plan.io/issues/1944>`_
        * `Pulp-2-Tests #91
          <https://github.com/PulpQE/Pulp-2-Tests/issues/91>`_

        Do the following:

        1. Create and sync a repository containing
           ``yum_repo_metadata_file``.
        2. Create another repo and copy yum metadata from
           first repo to second repo.
        3. Publish repo 2.
        4. Remove the metadata units from the first repo. Delete
           orphan packages.
        5. Publish repo 2 again and check whether the metadata is
           present in the second repo still.
        """
        cfg = config.get_config()
        client = api.Client(cfg, api.json_handler)
        body = gen_repo(
            importer_config={'feed': RPM_YUM_METADATA_FILE},
            distributors=[gen_distributor()],
        )
        repo_1 = client.post(REPOSITORY_PATH, body)
        self.addCleanup(client.delete, repo_1['_href'])
        sync_repo(cfg, repo_1)
        repo_1 = client.get(repo_1['_href'], params={'details': True})

        # Create a second repository.
        body = gen_repo(distributors=[gen_distributor()])
        repo_2 = client.post(REPOSITORY_PATH, body)
        repo_2 = client.get(repo_2['_href'], params={'details': True})
        self.addCleanup(client.delete, repo_2['_href'])

        # Copy data to second repository.
        client.post(
            urljoin(repo_2['_href'], 'actions/associate/'),
            {
                'source_repo_id': repo_1['id'],
                'override_config': {'recursive': True},
                'criteria': {'filters': {}, 'type_ids': ['yum_repo_metadata_file']},
            },
        )

        # Publish repo 2
        publish_repo(cfg, repo_2)
        # Removing metadata from repo 1 and deleting orphans.
        client.post(
            urljoin(repo_1['_href'], 'actions/unassociate/'),
            {'criteria': {'filters': {}}},
        )
        repo_1 = client.get(repo_1['_href'], params={'details': True})
        client.delete(ORPHANS_PATH)
        # Publish repo 2 again
        publish_repo(cfg, repo_2)
        repo_2 = client.get(repo_2['_href'], params={'details': True})

        # retrieve repodata of the published repo
        xml_element = get_repodata_repomd_xml(cfg, repo_2['distributors'][0])
        xpath = '{{{namespace}}}data'.format(namespace=RPM_NAMESPACES['metadata/repo'])
        yum_meta_data_element = [
            element
            for element in xml_element.findall(xpath)
            if element.attrib['type'] == 'productid'
        ]
        self.assertNotIn('yum_repo_metadata_file', repo_1['content_unit_counts'])
        self.assertEqual(repo_2['content_unit_counts']['yum_repo_metadata_file'], 1)
        self.assertGreater(len(yum_meta_data_element), 0)


@pytest.mark.recursive_conservative
class CopyConservativeTestCase(unittest.TestCase):
    """Test ``recursive`` and ``recursive_conservative`` flags during copy.

    RPM packages used in this test case::

        chimpanzee
        ├── squirrel
        │   ├── camel
        │   └── fox
        └── walrus

    chimpanzee has dependencies: squirrel and walrus RPM packages.
    squirrel has dependencies: camel and fox RPM packages.

    walrus package has 2 different versions:  ``0.71`` and ``5.21``.

    This test targets the following issues:

    * `Pulp #4152 <https://pulp.plan.io/issues/4152>`_
    * `Pulp #4269 <https://pulp.plan.io/issues/4269>`_
    * `Pulp #4543 <https://pulp.plan.io/issues/4543>`_
    """

    @classmethod
    def setUpClass(cls):
        """Create class-wide variables."""
        cls.cfg = config.get_config()
        if cls.cfg.pulp_version < Version('2.18.1'):
            raise unittest.SkipTest('This test requires Pulp 2.18.1 or newer.')
        cls.client = api.Client(cls.cfg, api.json_handler)

    def test_recursive_noconservative_nodependency(self):
        """Recursive, non-conservative, and no old dependency.

        Do the following:

        1. Copy ``chimpanzee`` RPM package from repository A to B using:
           ``recursive`` as True, ``recursive_conservative`` as False, and no
           older version of walrus package is present on the repo B before
           the copy.
        2. Assert that total number of RPM of units copied is equal to ``5``,
           and the walrus package version is equal to ``5.21``.
        """
        repo = self.copy_units(True, False, False)
        versions = [
            unit['metadata']['version']
            for unit in search_units(self.cfg, repo, {'type_ids': ['rpm']})
            if unit['metadata']['name'] == 'walrus'
        ]
        self.assertEqual(len(versions), 1, versions)
        self.assertEqual(versions[0], '5.21', versions)

        dst_unit_ids = [
            unit['metadata']['name']
            for unit in search_units(self.cfg, repo, {'type_ids': ['rpm']})
        ]
        self.assertEqual(len(dst_unit_ids), 5, dst_unit_ids)

    def test_recursive_conservative_nodependency(self):
        """Recursive, conservative, and no old dependency.

        Do the following:

        1. Copy ``chimpanzee`` RPM package from repository A to B using:
           ``recursive`` as True, ``recursive_conservative`` as True, and no
           older version of walrus package is present on the repo B before
           the copy.
        2. Assert that total number of RPM of units copied is equal to ``5``,
           and the walrus package version is equal to ``5.21``.
        """
        repo = self.copy_units(True, True, False)
        versions = [
            unit['metadata']['version']
            for unit in search_units(self.cfg, repo, {'type_ids': ['rpm']})
            if unit['metadata']['name'] == 'walrus'
        ]
        self.assertEqual(len(versions), 1, versions)
        self.assertEqual(versions[0], '5.21', versions)

        dst_unit_ids = [
            unit['metadata']['name']
            for unit in search_units(self.cfg, repo, {'type_ids': ['rpm']})
        ]
        self.assertEqual(len(dst_unit_ids), 5, dst_unit_ids)

    def test_recursive_conservative_dependency(self):
        """Recursive, conservative and with old dependency.

        Do the following:

        1. Copy ``chimpanzee`` RPM package from repository A to B using:
           ``recursive`` as True, ``recursive_conservative`` as True, and an
           older version of walrus package is present on the repo B before
           the copy.
        2. Assert that total number of RPM of units is equal to ``5``
           and the walrus package version is equal to ``0.71``.
        """
        repo = self.copy_units(True, True, True)
        versions = [
            unit['metadata']['version']
            for unit in search_units(self.cfg, repo, {'type_ids': ['rpm']})
            if unit['metadata']['name'] == 'walrus'
        ]
        self.assertEqual(len(versions), 1, versions)
        self.assertEqual(versions[0], '0.71', versions)

        dst_unit_ids = [
            unit['metadata']['name']
            for unit in search_units(self.cfg, repo, {'type_ids': ['rpm']})
        ]
        self.assertEqual(len(dst_unit_ids), 5, dst_unit_ids)

    def test_norecursive_conservative_dependency(self):
        """Non-recursive, conservative, with old dependency.

        Do the following:

        1. Copy ``chimpanzee`` RPM package from repository A to B using:
           ``recursive`` as False, ``recursive_conservative`` as True, and
           an older version of walrus package is present on the repo B
           before the copy.
        2. Assert that total number of RPM of units is equal to ``5``,
           and the walrus package version is equal to ``0.71``.
        """
        repo = self.copy_units(False, True, True)
        versions = [
            unit['metadata']['version']
            for unit in search_units(self.cfg, repo, {'type_ids': ['rpm']})
            if unit['metadata']['name'] == 'walrus'
        ]
        self.assertEqual(len(versions), 1, versions)
        self.assertEqual(versions[0], '0.71', versions)

        dst_unit_ids = [
            unit['metadata']['name']
            for unit in search_units(self.cfg, repo, {'type_ids': ['rpm']})
        ]
        self.assertEqual(len(dst_unit_ids), 5, dst_unit_ids)

    def test_norecursive_noconservative_nodependency(self):
        """Non-recursive, non-conservative, and no old dependency.

        Do the following:

        1. Copy ``chimpanzee`` RPM package from repository A to B using:
           ``recursive`` as False, ``recursive_conservative`` as False, and no
           older version of walrus package is present on the repo B before
           the copy.
        2. Assert that total number of RPM of units copied is equal to ``1``.
        """
        repo = self.copy_units(False, False, False)
        dst_unit_ids = [
            unit['metadata']['name']
            for unit in search_units(self.cfg, repo, {'type_ids': ['rpm']})
        ]
        self.assertEqual(len(dst_unit_ids), 1, dst_unit_ids)

    def test_recursive_noconservative_dependency(self):
        """Recursive, non-conservative, and ``walrus-0.71`` on B.

        Do the following:

        1. Copy ``chimpanzee`` RPM package from repository A to B using:
           ``recursive`` as True, ``recursive_conservative`` as False, and an
           older version of walrus package is present on the repo B before
           the copy.
        2. Assert that total number of RPM of units copied is equal to ``6``,
           and the walrus package version is equal to both ``5.21`` and
           ``0.71``.

        Additional permutation added as ``--recursive`` should ensure
        the ``latest`` version of the RPM is also copied.
        """
        repo = self.copy_units(True, False, True)
        # Versions of modules expected to be returned
        expected_versions = ['5.21', '0.71']
        # Search and return RPM packages after copied on B
        versions = [
            unit['metadata']['version']
            for unit in search_units(self.cfg, repo, {'type_ids': ['rpm']})
            if unit['metadata']['name'] == 'walrus'
        ]
        self.assertEqual(len(versions), 2, versions)
        self.assertEqual(sorted(versions), sorted(expected_versions), versions)
        dst_unit_ids = [
            unit['metadata']['name']
            for unit in search_units(self.cfg, repo, {'type_ids': ['rpm']})
        ]
        # Expect to find one more unit since old and new version of
        # walrus are now on repo B
        self.assertEqual(len(dst_unit_ids), 6, dst_unit_ids)

    def copy_units(self, recursive, recursive_conservative, old_dependency):
        """Copy units using ``recursive`` and  ``recursive_conservative``."""
        repos = []
        body = gen_repo(
            importer_config={'feed': RPM_UNSIGNED_FEED_URL},
            distributors=[gen_distributor()],
        )
        repos.append(self.client.post(REPOSITORY_PATH, body))
        self.addCleanup(self.client.delete, repos[0]['_href'])
        sync_repo(self.cfg, repos[0])
        repos.append(self.client.post(REPOSITORY_PATH, gen_repo()))
        self.addCleanup(self.client.delete, repos[1]['_href'])

        # `old_dependency` will import an older version, `0.71` of walrus to
        # the destiny repository.
        if old_dependency:
            rpm = utils.http_get(RPM_WITH_OLD_VERSION_URL)
            upload_import_unit(self.cfg, rpm, {'unit_type_id': 'rpm'}, repos[1])
            units = search_units(self.cfg, repos[1], {'type_ids': ['rpm']})
            self.assertEqual(len(units), 1, units)

        self.client.post(
            urljoin(repos[1]['_href'], 'actions/associate/'),
            {
                'source_repo_id': repos[0]['id'],
                'override_config': {
                    'recursive': recursive,
                    'recursive_conservative': recursive_conservative,
                },
                'criteria': {
                    'filters': {'unit': {'name': 'chimpanzee'}},
                    'type_ids': ['rpm'],
                },
            },
        )
        return self.client.get(repos[1]['_href'], params={'details': True})


@pytest.mark.recursive_conservative
class MultipleSourceRepositoriesRecursiveCopyTestCase(unittest.TestCase):
    """Test multiple source repositories recursive copy.

    Repository 1 is a modular repository. There are 2 RPM dependencies
    missing from repository 1. ``stork`` and ``shark`` RPMS.

    Repository 2 is non-modular repository and has the RPMS ``stork``
    and ``shark`` missing from repository 1.

    [walrus-0.71] - Module - repository 1
    ├── walrus - RPM - repository 1
        └── whale - RPM present on repository 1
            ├── shark - RPM present on repository 2
            └── stork - RPM present on repository 2
                                                                    
    This test targets the following issues:

    * `Pulp #5067 <https://pulp.plan.io/issues/5067>`_
    * `Pulp #5242 <https://pulp.plan.io/issues/5242>`_
    * `Pulp #5449 <https://pulp.plan.io/issues/5449>`_
    """

    @classmethod
    def setUpClass(cls):
        """Create class-wide variables."""
        cls.cfg = config.get_config()
        if cls.cfg.pulp_version < Version('2.21'):
            raise unittest.SkipTest('This test requires Pulp 2.21 or newer.')
        cls.client = api.Client(cls.cfg, api.json_handler)

    def test_additional_repos_copy_one_destination(self):
        """Multiple source repositories recursive copy - one repo destination.

        1. Copy ``walrus - 0.71`` module to repository 3, and all
           the dependencies RPMS should be solved and copied as well.
        """
        repo_1 = self.create_sync_repo(RPM_WITH_MODULES_MODIFIED_FEED_URL)
        repo_2 = self.create_sync_repo(RPM_UNSIGNED_MODIFIED_FEED_URL)

        repo_3 = self.client.post(REPOSITORY_PATH, gen_repo())
        self.addCleanup(self.client.delete, repo_3['_href'])

        self.client.post(
            urljoin(repo_3['_href'], 'actions/associate/'),
            {
                'source_repo_id': repo_1['id'],
                'override_config': {
                    'recursive_conservative': True,
                    'additional_repos': {repo_2['id']: repo_3['id']},
                },
                'criteria': {
                    'filters': {
                        'unit': {
                            '$and': [
                                {
                                    'name': MODULE_FIXTURES_PACKAGE_STREAM['name'],
                                    'stream': MODULE_FIXTURES_PACKAGE_STREAM['stream'],
                                }
                            ]
                        }
                    },
                    'type_ids': ['modulemd'],
                },
            },
        )

        repo_3 = self.client.get(repo_3['_href'], params={'details': True})

        repo_3_rpms = sorted(
            [
                unit['metadata']['filename']
                for unit in search_units(self.cfg, repo_3, {'type_ids': ['rpm']})
            ]
        )

        self.assertEqual(repo_3_rpms, RPM_PACKAGES_MULTIPLE_REPOS, repo_3_rpms)
        self.assertEqual(repo_3['content_unit_counts']['modulemd'], 1, repo_3)
        self.assertEqual(repo_3['content_unit_counts']['modulemd_defaults'], 1, repo_3)

    def test_additional_repos_copy_two_destinations(self):
        """Multiple source repositories recursive copy - two repos destination.

        1. Copy ``walrus - 0.71`` module to repository 3 and the
           dependencies present on the repository 1 will be copied as well.
           The other dependencies present on the repository 2, ``shark``
           and ``stork`` will be copied to the repository 4.
        """
        repo_1 = self.create_sync_repo(RPM_WITH_MODULES_MODIFIED_FEED_URL)
        repo_2 = self.create_sync_repo(RPM_UNSIGNED_MODIFIED_FEED_URL)

        repo_3 = self.client.post(REPOSITORY_PATH, gen_repo())
        self.addCleanup(self.client.delete, repo_3['_href'])

        repo_4 = self.client.post(REPOSITORY_PATH, gen_repo())
        self.addCleanup(self.client.delete, repo_4['_href'])

        self.client.post(
            urljoin(repo_3['_href'], 'actions/associate/'),
            {
                'source_repo_id': repo_1['id'],
                'override_config': {
                    'recursive_conservative': True,
                    'additional_repos': {repo_2['id']: repo_4['id']},
                },
                'criteria': {
                    'filters': {
                        'unit': {
                            '$and': [
                                {
                                    'name': MODULE_FIXTURES_PACKAGE_STREAM['name'],
                                    'stream': MODULE_FIXTURES_PACKAGE_STREAM['stream'],
                                }
                            ]
                        }
                    },
                    'type_ids': ['modulemd'],
                },
            },
        )

        repo_3 = self.client.get(repo_3['_href'], params={'details': True})
        repo_4 = self.client.get(repo_4['_href'], params={'details': True})

        repo_3_rpms = sorted(
            [
                unit['metadata']['filename']
                for unit in search_units(self.cfg, repo_3, {'type_ids': ['rpm']})
            ]
        )

        repo_4_rpms = sorted(
            [
                unit['metadata']['filename']
                for unit in search_units(self.cfg, repo_4, {'type_ids': ['rpm']})
            ]
        )

        self.assertEqual(repo_3['content_unit_counts']['modulemd'], 1, repo_3)
        self.assertEqual(repo_3['content_unit_counts']['modulemd_defaults'], 1, repo_3)
        self.assertEqual(repo_3_rpms, RPM_PACKAGES_MULTIPLE_REPOS[-2:], repo_3_rpms)

        self.assertEqual(len(repo_4['content_unit_counts']), 1, repo_4)
        self.assertEqual(repo_4_rpms, RPM_PACKAGES_MULTIPLE_REPOS[:-2], repo_4_rpms)

    def test_additional_repos_errata(self):
        """Copy errata using additional repos as source.

        Copy errata ``RHEA-2012:0059`` from repository 1 to repository 3.
        Repository 1 does not have the package ``kangaroo-0.3-1.noarch.rpm``
        required by the errata. This package is present on the repository 2.
        Pulp should copy the errata and solve the dependency chain for
        repository 3.
        """
        if not selectors.bug_is_fixed(5449, self.cfg.pulp_version):
            self.skipTest('https://pulp.plan.io/issues/5449')

        for _ in range(2):
            repo_1 = self.create_sync_repo(RPM_WITH_MODULES_MODIFIED_FEED_URL)
            repo_2 = self.create_sync_repo(RPM_UNSIGNED_MODIFIED_FEED_URL)

            repo_3 = self.client.post(REPOSITORY_PATH, gen_repo())
            self.addCleanup(self.client.delete, repo_3['_href'])

            self.client.post(
                urljoin(repo_3['_href'], 'actions/associate/'),
                {
                    'source_repo_id': repo_1['id'],
                    'override_config': {
                        'recursive_conservative': True,
                        'additional_repos': {repo_2['id']: repo_3['id']},
                    },
                    'criteria': {
                        'filters': {
                            'unit': {'id': MODULE_FIXTURES_ERRATA['errata_id']}
                        },
                        'type_ids': ['erratum'],
                    },
                },
            )

            repo_3 = self.client.get(repo_3['_href'], params={'details': True})

            repo_3_rpms = sorted(
                [
                    unit['metadata']['filename']
                    for unit in search_units(self.cfg, repo_3, {'type_ids': ['rpm']})
                ]
            )

            self.assertEqual(
                repo_3_rpms, MODULE_FIXTURES_ERRATA['packages'], repo_3_rpms
            )

            self.assertEqual(
                repo_3['content_unit_counts']['erratum'],
                MODULE_FIXTURES_ERRATA['errata_count'],
                repo_3['content_unit_counts'],
            )

            self.assertEqual(
                repo_3['content_unit_counts']['modulemd'],
                MODULE_FIXTURES_ERRATA['modules_count'],
                repo_3['content_unit_counts'],
            )

            self.assertEqual(
                repo_3['content_unit_counts']['modulemd_defaults'],
                MODULE_FIXTURES_ERRATA['module_defaults_count'],
                repo_3['content_unit_counts'],
            )

    def create_sync_repo(self, feed):
        """Create and sync a repository given a feed."""
        body = gen_repo(
            importer_config={'feed': feed}, distributors=[gen_distributor()]
        )
        # Using on_demand since its the default policy used by Satellite
        body['importer_config']['download_policy'] = 'on_demand'
        repo = self.client.post(REPOSITORY_PATH, body)
        self.addCleanup(self.client.delete, repo['_href'])
        sync_repo(self.cfg, repo)
        return self.client.get(repo['_href'], params={'details': True})
