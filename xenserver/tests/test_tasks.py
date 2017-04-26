"""
Some quick and dirty tests for a very small subset of the code.
"""

import pytest

from xenserver.models import XenVM, XenServer
from xenserver import tasks
from xenserver.tests.helpers import XenServerHelper
from xenserver.tests.matchers import (
    ExpectedXenServerVM, ExpectedXenServerVIF, ExtractValues, MatchSorted)


@pytest.fixture
def xs_helper(monkeypatch):
    """
    Provide a XenServerHelper instance and monkey-patch xenserver.tasks to use
    sessions from that instance instead of making real API calls.
    """
    xshelper = XenServerHelper()
    monkeypatch.setattr(tasks, 'getSession', xshelper.get_session)
    return xshelper


class TaskCatcher(object):
    def __init__(self, monkeypatch):
        self.mp = monkeypatch

    def catch_async(self, task, f=lambda *a: a):
        """
        Return a list that will be populated with the args of any call to the
        given task.
        """
        calls = []
        self.mp.setattr(task, 'apply_async', lambda *a: calls.append(f(*a)))
        return calls

    def catch_updateServer(self):
        """
        Special case of catch_async for updateServer.
        """
        return self.catch_async(
            tasks.updateServer, lambda args, kwargs: args[0].hostname)


def updateServer_argf(args, kwargs):
    return args[0].hostname


@pytest.fixture
def task_catcher(monkeypatch):
    """
    Monkey-patch the given task's apply_async() method to add calls to a
    list instead of queueing the task.
    """
    return TaskCatcher(monkeypatch)


@pytest.mark.django_db
class TestCreateVM(object):
    """
    Test xenserver.tasks.create_vm task.
    """

    def setup_vm(self, name, template, status="Provisioning", **kw):
        params = {
            "sockets": template.cores,
            "memory": template.memory,
        }
        params.update(kw)
        return XenVM.objects.create(
            name="foovm", status="Provisioning", template=template, **params)

    def extract_VIFs(self, xenserver, VM_ref, spec):
        """
        Get the VIFs for the given VM and match them to a list of (network,
        VIF) ref pairs.
        """
        assert MatchSorted(spec) == xenserver.list_network_VIFs_for_VM(VM_ref)

    def extract_VBDs(self, xenserver, VM_ref, spec):
        """
        Get the VBDs for the given VM and match them to a list of (SR, VBD)
        ref pairs.
        """
        assert MatchSorted(spec) == xenserver.list_SR_VBDs_for_VM(VM_ref)

    def expected_vm(self, template, local_SR, VIFs, VBDs, **kw):
        """
        Build an ExpectedXenServerVM object with some default parameters.
        """
        params = {
            "PV_args": " -- quiet console=hvc0",
            "name_label": "None.None",
            "VCPUs_max": "1",
            "VCPUs_at_startup": "1",
            "memory_static_max": str(template.memory*1024*1024),
            "memory_dynamic_max": str(template.memory*1024*1024),
            "suspend_SR": local_SR,
        }
        return ExpectedXenServerVM(
            VIFs=MatchSorted(VIFs), VBDs=MatchSorted(VBDs), **params)

    xapi_versions = pytest.mark.parametrize('xapi_version', [(1, 1), (1, 2)])

    @xapi_versions
    def test_create_vm_simple(self, xapi_version, xs_helper):
        """
        We can create a new VM using mostly default values.
        """
        xsh, xs = xs_helper.new_host('xenserver01.local', xapi_version)
        template = xs_helper.db_template("footempl")
        vm = self.setup_vm("foovm", template)

        assert vm.xsref == ''
        assert xsh.api.VMs == {}

        tasks.create_vm.apply(
            [vm, xs, template, None, None, None, None, None, None],
            {'extra_network_bridges': []})

        vm.refresh_from_db()
        assert vm.xsref != ''

        # Make sure the right VIFs and VBDs were created and extract their
        # reference values.
        ev = ExtractValues("VIF", "iso_VBD", "local_VBD")
        self.extract_VIFs(xsh.api, vm.xsref, [(xsh.net['eth0'], ev.VIF)])
        self.extract_VBDs(xsh.api, vm.xsref, [
            (xsh.sr['iso'], ev.iso_VBD),
            (xsh.sr['local'], ev.local_VBD),
        ])

        # The VM data structure should match the values we passed to
        # create_vm().
        assert xsh.api.VMs.keys() == [vm.xsref]
        assert self.expected_vm(
            template, xsh.sr['local'], VIFs=[ev.VIF],
            VBDs=[ev.iso_VBD, ev.local_VBD],
        ) == xsh.api.VMs[vm.xsref]

        # The VIF data structures should match the values we passed to
        # create_vm().
        assert xsh.api.VIFs.keys() == [ev.VIF.value]
        assert ExpectedXenServerVIF(
            device="0", VM=vm.xsref, network=xsh.net['eth0'],
        ) == xsh.api.VIFs[ev.VIF.value]

        # The VM should be started.
        assert xsh.api.VM_operations == [(vm.xsref, "start")]

    @xapi_versions
    def test_create_vm_second_vif(self, xapi_version, xs_helper):
        """
        We can create a new VM with a second VIF.
        """
        xsh, xs = xs_helper.new_host('xenserver01.local', xapi_version)
        template = xs_helper.db_template("footempl")
        vm = self.setup_vm("foovm", template)

        assert vm.xsref == ''
        assert xsh.api.VMs == {}

        tasks.create_vm.apply(
            [vm, xs, template, None, None, None, None, None, None],
            {'extra_network_bridges': ['xenbr1']})

        vm.refresh_from_db()
        assert vm.xsref != ''

        # Make sure the right VIFs and VBDs were created and extract their
        # reference values.
        ev = ExtractValues("pub_VIF", "prv_VIF", "iso_VBD", "local_VBD")
        self.extract_VIFs(xsh.api, vm.xsref, [
            (xsh.net['eth0'], ev.pub_VIF),
            (xsh.net['eth1'], ev.prv_VIF),
        ])
        self.extract_VBDs(xsh.api, vm.xsref, [
            (xsh.sr['iso'], ev.iso_VBD),
            (xsh.sr['local'], ev.local_VBD),
        ])

        # The VM data structure should match the values we passed to
        # create_vm().
        assert xsh.api.VMs.keys() == [vm.xsref]
        assert self.expected_vm(
            template, xsh.sr['local'], VIFs=[ev.pub_VIF, ev.prv_VIF],
            VBDs=[ev.iso_VBD, ev.local_VBD],
        ) == xsh.api.VMs[vm.xsref]

        # The VIF data structures should match the values we passed to
        # create_vm().
        assert MatchSorted([ev.pub_VIF, ev.prv_VIF]) == xsh.api.VIFs.keys()
        assert ExpectedXenServerVIF(
            device="0", VM=vm.xsref, network=xsh.net['eth0'],
        ) == xsh.api.VIFs[ev.pub_VIF.value]
        assert ExpectedXenServerVIF(
            device="1", VM=vm.xsref, network=xsh.net['eth1'],
        ) == xsh.api.VIFs[ev.prv_VIF.value]

        # The VM should be started.
        assert xsh.api.VM_operations == [(vm.xsref, "start")]


@pytest.mark.django_db
class TestUpdateVms(object):
    """
    Test xenserver.tasks.updateVms task.
    """

    def test_no_servers(self, xs_helper, task_catcher):
        """
        Nothing to do if we have no servers.
        """
        us_calls = task_catcher.catch_updateServer()
        tasks.updateVms.apply()
        assert us_calls == []

    def test_one_server(self, xs_helper, task_catcher):
        """
        A single server will be updated.
        """
        xs_helper.new_host('xs01.local')
        us_calls = task_catcher.catch_updateServer()
        tasks.updateVms.apply()
        assert us_calls == ['xs01.local']

    def test_three_servers(self, xs_helper, task_catcher):
        """
        Multiple servers will be updated.
        """
        xs_helper.new_host('xs01.local')
        xs_helper.new_host('xs02.local')
        xs_helper.new_host('xs03.local')
        us_calls = task_catcher.catch_updateServer()
        tasks.updateVms.apply()
        assert sorted(us_calls) == ['xs01.local', 'xs02.local', 'xs03.local']
