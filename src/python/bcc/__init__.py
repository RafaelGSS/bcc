# Copyright 2015 PLUMgrid
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function
import atexit
import ctypes as ct
import fcntl
import json
import multiprocessing
import os
import re
import struct
import sys
basestring = (unicode if sys.version_info[0] < 3 else str)

from .libbcc import lib, _CB_TYPE, bcc_symbol
from .table import Table
from .tracepoint import Tracepoint
from .perf import Perf
from .usyms import ProcessSymbols

_kprobe_limit = 1000
_num_open_probes = 0

# for tests
def _get_num_open_probes():
    global _num_open_probes
    return _num_open_probes

TRACEFS = "/sys/kernel/debug/tracing"

DEBUG_LLVM_IR = 0x1
DEBUG_BPF = 0x2
DEBUG_PREPROCESSOR = 0x4

class SymbolCache(object):
    def __init__(self, pid):
        self.cache = lib.bcc_symcache_new(pid)

    def resolve(self, addr):
        sym = bcc_symbol()
        psym = ct.pointer(sym)
        if lib.bcc_symcache_resolve(self.cache, addr, psym) < 0:
            return "[unknown]", 0
        return sym.name.decode(), sym.offset

    def resolve_name(self, name):
        addr = ct.c_ulonglong()
        if lib.bcc_symcache_resolve_name(self.cache, name, ct.pointer(addr)) < 0:
            return -1
        return addr.value

class BPF(object):
    SOCKET_FILTER = 1
    KPROBE = 2
    SCHED_CLS = 3
    SCHED_ACT = 4
    TRACEPOINT = 5

    _probe_repl = re.compile("[^a-zA-Z0-9_]")
    _sym_caches = {}

    _auto_includes = {
        "linux/time.h": ["time"],
        "linux/fs.h": ["fs", "file"],
        "linux/blkdev.h": ["bio", "request"],
        "linux/slab.h": ["alloc"],
        "linux/netdevice.h": ["sk_buff", "net_device"]
    }

    @classmethod
    def generate_auto_includes(cls, program_words):
        """
        Generates #include statements automatically based on a set of
        recognized types such as sk_buff and bio. The input is all the words
        that appear in the BPF program, and the output is a (possibly empty)
        string of #include statements, such as "#include <linux/fs.h>".
        """
        headers = ""
        for header, keywords in cls._auto_includes.items():
            for keyword in keywords:
                for word in program_words:
                    if keyword in word and header not in headers:
                        headers += "#include <%s>\n" % header
        return headers

    # defined for compatibility reasons, to be removed
    Table = Table

    class Function(object):
        def __init__(self, bpf, name, fd):
            self.bpf = bpf
            self.name = name
            self.fd = fd

    @staticmethod
    def _find_file(filename):
        """ If filename is invalid, search in ./ of argv[0] """
        if filename:
            if not os.path.isfile(filename):
                t = "/".join([os.path.abspath(os.path.dirname(sys.argv[0])), filename])
                if os.path.isfile(t):
                    filename = t
                else:
                    raise Exception("Could not find file %s" % filename)
        return filename

    def __init__(self, src_file="", hdr_file="", text=None, cb=None, debug=0,
            cflags=[], usdt=None):
        """Create a a new BPF module with the given source code.

        Note:
            All fields are marked as optional, but either `src_file` or `text`
            must be supplied, and not both.

        Args:
            src_file (Optional[str]): Path to a source file for the module
            hdr_file (Optional[str]): Path to a helper header file for the `src_file`
            text (Optional[str]): Contents of a source file for the module
            debug (Optional[int]): Flags used for debug prints, can be |'d together
                DEBUG_LLVM_IR: print LLVM IR to stderr
                DEBUG_BPF: print BPF bytecode to stderr
                DEBUG_PREPROCESSOR: print Preprocessed C file to stderr
        """

        self.open_kprobes = {}
        self.open_uprobes = {}
        self.open_tracepoints = {}
        self.tracefile = None
        atexit.register(self.cleanup)

        self._reader_cb_impl = _CB_TYPE(BPF._reader_cb)
        self._user_cb = cb
        self.debug = debug
        self.funcs = {}
        self.tables = {}
        cflags_array = (ct.c_char_p * len(cflags))()
        for i, s in enumerate(cflags): cflags_array[i] = s.encode("ascii")
        if usdt and text: text = usdt.get_text() + text

        if text:
            self.module = lib.bpf_module_create_c_from_string(text.encode("ascii"),
                    self.debug, cflags_array, len(cflags_array))
        else:
            src_file = BPF._find_file(src_file)
            hdr_file = BPF._find_file(hdr_file)
            if src_file.endswith(".b"):
                self.module = lib.bpf_module_create_b(src_file.encode("ascii"),
                        hdr_file.encode("ascii"), self.debug)
            else:
                self.module = lib.bpf_module_create_c(src_file.encode("ascii"),
                        self.debug, cflags_array, len(cflags_array))

        if not self.module:
            raise Exception("Failed to compile BPF module %s" % src_file)

        if usdt: usdt.attach_uprobes(self)

        # If any "kprobe__" or "tracepoint__" prefixed functions were defined,
        # they will be loaded and attached here.
        self._trace_autoload()

    def load_funcs(self, prog_type=KPROBE):
        """load_funcs(prog_type=KPROBE)

        Load all functions in this BPF module with the given type.
        Returns a list of the function handles."""

        fns = []
        for i in range(0, lib.bpf_num_functions(self.module)):
            func_name = lib.bpf_function_name(self.module, i).decode()
            fns.append(self.load_func(func_name, prog_type))

        return fns

    def load_func(self, func_name, prog_type):
        if func_name in self.funcs:
            return self.funcs[func_name]

        if not lib.bpf_function_start(self.module, func_name.encode("ascii")):
            raise Exception("Unknown program %s" % func_name)

        log_buf = ct.create_string_buffer(65536) if self.debug else None

        fd = lib.bpf_prog_load(prog_type,
                lib.bpf_function_start(self.module, func_name.encode("ascii")),
                lib.bpf_function_size(self.module, func_name.encode("ascii")),
                lib.bpf_module_license(self.module),
                lib.bpf_module_kern_version(self.module),
                log_buf, ct.sizeof(log_buf) if log_buf else 0)

        if self.debug & DEBUG_BPF and log_buf.value:
            print(log_buf.value.decode(), file=sys.stderr)

        if fd < 0:
            if self.debug & DEBUG_BPF:
                errstr = os.strerror(ct.get_errno())
                raise Exception("Failed to load BPF program %s: %s" %
                                (func_name, errstr))
            else:
                raise Exception("Failed to load BPF program %s" % func_name)

        fn = BPF.Function(self, func_name, fd)
        self.funcs[func_name] = fn

        return fn

    def dump_func(self, func_name):
        """
        Return the eBPF bytecodes for the specified function as a string
        """
        if not lib.bpf_function_start(self.module, func_name.encode("ascii")):
            raise Exception("Unknown program %s" % func_name)

        start, = lib.bpf_function_start(self.module, func_name.encode("ascii")),
        size, = lib.bpf_function_size(self.module, func_name.encode("ascii")),
        return ct.string_at(start, size)

    str2ctype = {
        u"_Bool": ct.c_bool,
        u"char": ct.c_char,
        u"wchar_t": ct.c_wchar,
        u"unsigned char": ct.c_ubyte,
        u"short": ct.c_short,
        u"unsigned short": ct.c_ushort,
        u"int": ct.c_int,
        u"unsigned int": ct.c_uint,
        u"long": ct.c_long,
        u"unsigned long": ct.c_ulong,
        u"long long": ct.c_longlong,
        u"unsigned long long": ct.c_ulonglong,
        u"float": ct.c_float,
        u"double": ct.c_double,
        u"long double": ct.c_longdouble
    }
    @staticmethod
    def _decode_table_type(desc):
        if isinstance(desc, basestring):
            return BPF.str2ctype[desc]
        anon = []
        fields = []
        for t in desc[1]:
            if len(t) == 2:
                fields.append((t[0], BPF._decode_table_type(t[1])))
            elif len(t) == 3:
                if isinstance(t[2], list):
                    fields.append((t[0], BPF._decode_table_type(t[1]) * t[2][0]))
                elif isinstance(t[2], int):
                    fields.append((t[0], BPF._decode_table_type(t[1]), t[2]))
                elif isinstance(t[2], basestring) and (
                        t[2] == u"union" or t[2] == u"struct"):
                    name = t[0]
                    if name == "":
                        name = "__anon%d" % len(anon)
                        anon.append(name)
                    fields.append((name, BPF._decode_table_type(t)))
                else:
                    raise Exception("Failed to decode type %s" % str(t))
            else:
                raise Exception("Failed to decode type %s" % str(t))
        base = ct.Structure
        if len(desc) > 2:
            if desc[2] == u"union":
                base = ct.Union
            elif desc[2] == u"struct":
                base = ct.Structure
        cls = type(str(desc[0]), (base,), dict(_anonymous_=anon,
            _fields_=fields))
        return cls

    def get_table(self, name, keytype=None, leaftype=None, reducer=None):
        map_id = lib.bpf_table_id(self.module, name.encode("ascii"))
        map_fd = lib.bpf_table_fd(self.module, name.encode("ascii"))
        if map_fd < 0:
            raise KeyError
        if not keytype:
            key_desc = lib.bpf_table_key_desc(self.module, name.encode("ascii"))
            if not key_desc:
                raise Exception("Failed to load BPF Table %s key desc" % name)
            keytype = BPF._decode_table_type(json.loads(key_desc.decode()))
        if not leaftype:
            leaf_desc = lib.bpf_table_leaf_desc(self.module, name.encode("ascii"))
            if not leaf_desc:
                raise Exception("Failed to load BPF Table %s leaf desc" % name)
            leaftype = BPF._decode_table_type(json.loads(leaf_desc.decode()))
        return Table(self, map_id, map_fd, keytype, leaftype, reducer=reducer)

    def __getitem__(self, key):
        if key not in self.tables:
            self.tables[key] = self.get_table(key)
        return self.tables[key]

    def __setitem__(self, key, leaf):
        self.tables[key] = leaf

    def __len__(self):
        return len(self.tables)

    def __delitem__(self, key):
        del self.tables[key]

    def __iter__(self):
        return self.tables.__iter__()

    def _reader_cb(self, pid, callchain_num, callchain):
        if self._user_cb:
            cc = tuple(callchain[i] for i in range(0, callchain_num))
            self._user_cb(pid, cc)

    @staticmethod
    def attach_raw_socket(fn, dev):
        if not isinstance(fn, BPF.Function):
            raise Exception("arg 1 must be of type BPF.Function")
        sock = lib.bpf_open_raw_sock(dev.encode("ascii"))
        if sock < 0:
            errstr = os.strerror(ct.get_errno())
            raise Exception("Failed to open raw device %s: %s" % (dev, errstr))
        res = lib.bpf_attach_socket(sock, fn.fd)
        if res < 0:
            errstr = os.strerror(ct.get_errno())
            raise Exception("Failed to attach BPF to device %s: %s"
                    % (dev, errstr))
        fn.sock = sock

    def _get_kprobe_functions(self, event_re):
        with open("%s/../kprobes/blacklist" % TRACEFS) as blacklist_file:
            blacklist = set([line.rstrip().split()[1] for line in
                    blacklist_file])
        fns = []
        with open("%s/available_filter_functions" % TRACEFS) as avail_file:
            for line in avail_file:
                fn = line.rstrip().split()[0]
                if re.match(event_re, fn) and fn not in blacklist:
                    fns.append(fn)
        self._check_probe_quota(len(fns))
        return fns

    def _check_probe_quota(self, num_new_probes):
        global _num_open_probes
        if _num_open_probes + num_new_probes > _kprobe_limit:
            raise Exception("Number of open probes would exceed global quota")

    def _add_kprobe(self, name, probe):
        global _num_open_probes
        self.open_kprobes[name] = probe
        _num_open_probes += 1

    def _del_kprobe(self, name):
        global _num_open_probes
        del self.open_kprobes[name]
        _num_open_probes -= 1

    def attach_kprobe(self, event="", fn_name="", event_re="",
            pid=-1, cpu=0, group_fd=-1):

        assert isinstance(event, str), "event must be a string"
        # allow the caller to glob multiple functions together
        if event_re:
            for line in self._get_kprobe_functions(event_re):
                try:
                    self.attach_kprobe(event=line, fn_name=fn_name, pid=pid,
                            cpu=cpu, group_fd=group_fd)
                except:
                    pass
            return

        self._check_probe_quota(1)
        fn = self.load_func(fn_name, BPF.KPROBE)
        ev_name = "p_" + event.replace("+", "_").replace(".", "_")
        desc = "p:kprobes/%s %s" % (ev_name, event)
        res = lib.bpf_attach_kprobe(fn.fd, ev_name.encode("ascii"),
                desc.encode("ascii"), pid, cpu, group_fd,
                self._reader_cb_impl, ct.cast(id(self), ct.py_object))
        res = ct.cast(res, ct.c_void_p)
        if not res:
            raise Exception("Failed to attach BPF to kprobe")
        self._add_kprobe(ev_name, res)
        return self

    def detach_kprobe(self, event):
        assert isinstance(event, str), "event must be a string"
        ev_name = "p_" + event.replace("+", "_").replace(".", "_")
        if ev_name not in self.open_kprobes:
            raise Exception("Kprobe %s is not attached" % event)
        lib.perf_reader_free(self.open_kprobes[ev_name])
        desc = "-:kprobes/%s" % ev_name
        res = lib.bpf_detach_kprobe(desc.encode("ascii"))
        if res < 0:
            raise Exception("Failed to detach BPF from kprobe")
        self._del_kprobe(ev_name)

    def attach_kretprobe(self, event="", fn_name="", event_re="",
            pid=-1, cpu=0, group_fd=-1):

        assert isinstance(event, str), "event must be a string"
        # allow the caller to glob multiple functions together
        if event_re:
            for line in self._get_kprobe_functions(event_re):
                try:
                    self.attach_kretprobe(event=line, fn_name=fn_name, pid=pid,
                            cpu=cpu, group_fd=group_fd)
                except:
                    pass
            return

        self._check_probe_quota(1)
        fn = self.load_func(fn_name, BPF.KPROBE)
        ev_name = "r_" + event.replace("+", "_").replace(".", "_")
        desc = "r:kprobes/%s %s" % (ev_name, event)
        res = lib.bpf_attach_kprobe(fn.fd, ev_name.encode("ascii"),
                desc.encode("ascii"), pid, cpu, group_fd,
                self._reader_cb_impl, ct.cast(id(self), ct.py_object))
        res = ct.cast(res, ct.c_void_p)
        if not res:
            raise Exception("Failed to attach BPF to kprobe")
        self._add_kprobe(ev_name, res)
        return self

    def detach_kretprobe(self, event):
        assert isinstance(event, str), "event must be a string"
        ev_name = "r_" + event.replace("+", "_").replace(".", "_")
        if ev_name not in self.open_kprobes:
            raise Exception("Kretprobe %s is not attached" % event)
        lib.perf_reader_free(self.open_kprobes[ev_name])
        desc = "-:kprobes/%s" % ev_name
        res = lib.bpf_detach_kprobe(desc.encode("ascii"))
        if res < 0:
            raise Exception("Failed to detach BPF from kprobe")
        self._del_kprobe(ev_name)

    @classmethod
    def _check_path_symbol(cls, module, symname, addr):
        sym = bcc_symbol()
        psym = ct.pointer(sym)
        if lib.bcc_resolve_symname(module.encode("ascii"),
                symname.encode("ascii"), addr or 0x0, psym) < 0:
            if not sym.module:
                raise Exception("could not find library %s" % module)
            raise Exception("could not determine address of symbol %s" % symname)
        return sym.module.decode(), sym.offset

    @staticmethod
    def find_library(libname):
        return lib.bcc_procutils_which_so(libname.encode("ascii")).decode()

    def attach_tracepoint(self, tp="", fn_name="", pid=-1, cpu=0, group_fd=-1):
        """attach_tracepoint(tp="", fn_name="", pid=-1, cpu=0, group_fd=-1)

        Run the bpf function denoted by fn_name every time the kernel tracepoint
        specified by 'tp' is hit. The optional parameters pid, cpu, and group_fd
        can be used to filter the probe. The tracepoint specification is simply
        the tracepoint category and the tracepoint name, separated by a colon.
        For example: sched:sched_switch, syscalls:sys_enter_bind, etc.

        To obtain a list of kernel tracepoints, use the tplist tool or cat the
        file /sys/kernel/debug/tracing/available_events.

        Example: BPF(text).attach_tracepoint("sched:sched_switch", "on_switch")
        """

        fn = self.load_func(fn_name, BPF.TRACEPOINT)
        (tp_category, tp_name) = tp.split(':')
        res = lib.bpf_attach_tracepoint(fn.fd, tp_category.encode("ascii"),
                tp_name.encode("ascii"), pid, cpu, group_fd,
                self._reader_cb_impl, ct.cast(id(self), ct.py_object))
        res = ct.cast(res, ct.c_void_p)
        if not res:
            raise Exception("Failed to attach BPF to tracepoint")
        self.open_tracepoints[tp] = res
        return self

    def detach_tracepoint(self, tp=""):
        """detach_tracepoint(tp="")

        Stop running a bpf function that is attached to the kernel tracepoint
        specified by 'tp'.

        Example: bpf.detach_tracepoint("sched:sched_switch")
        """

        if tp not in self.open_tracepoints:
            raise Exception("Tracepoint %s is not attached" % tp)
        lib.perf_reader_free(self.open_tracepoints[tp])
        (tp_category, tp_name) = tp.split(':')
        res = lib.bpf_detach_tracepoint(tp_category.encode("ascii"),
                                        tp_name.encode("ascii"))
        if res < 0:
            raise Exception("Failed to detach BPF from tracepoint")
        del self.open_tracepoints[tp]

    def _add_uprobe(self, name, probe):
        global _num_open_probes
        self.open_uprobes[name] = probe
        _num_open_probes += 1

    def _del_uprobe(self, name):
        global _num_open_probes
        del self.open_uprobes[name]
        _num_open_probes -= 1

    def attach_uprobe(self, name="", sym="", addr=None,
            fn_name="", pid=-1, cpu=0, group_fd=-1):
        """attach_uprobe(name="", sym="", addr=None, fn_name=""
                         pid=-1, cpu=0, group_fd=-1)

        Run the bpf function denoted by fn_name every time the symbol sym in
        the library or binary 'name' is encountered. The real address addr may
        be supplied in place of sym. Optional parameters pid, cpu, and group_fd
        can be used to filter the probe.

        Libraries can be given in the name argument without the lib prefix, or
        with the full path (/usr/lib/...). Binaries can be given only with the
        full path (/bin/sh).

        Example: BPF(text).attach_uprobe("c", "malloc")
                 BPF(text).attach_uprobe("/usr/bin/python", "main")
        """

        (path, addr) = BPF._check_path_symbol(name, sym, addr)

        self._check_probe_quota(1)
        fn = self.load_func(fn_name, BPF.KPROBE)
        ev_name = "p_%s_0x%x" % (self._probe_repl.sub("_", path), addr)
        desc = "p:uprobes/%s %s:0x%x" % (ev_name, path, addr)
        res = lib.bpf_attach_uprobe(fn.fd, ev_name.encode("ascii"),
                desc.encode("ascii"), pid, cpu, group_fd,
                self._reader_cb_impl, ct.cast(id(self), ct.py_object))
        res = ct.cast(res, ct.c_void_p)
        if not res:
            raise Exception("Failed to attach BPF to uprobe")
        self._add_uprobe(ev_name, res)
        return self

    def detach_uprobe(self, name="", sym="", addr=None):
        """detach_uprobe(name="", sym="", addr=None)

        Stop running a bpf function that is attached to symbol 'sym' in library
        or binary 'name'.
        """

        (path, addr) = BPF._check_path_symbol(name, sym, addr)
        ev_name = "p_%s_0x%x" % (self._probe_repl.sub("_", path), addr)
        if ev_name not in self.open_uprobes:
            raise Exception("Uprobe %s is not attached" % event)
        lib.perf_reader_free(self.open_uprobes[ev_name])
        desc = "-:uprobes/%s" % ev_name
        res = lib.bpf_detach_uprobe(desc.encode("ascii"))
        if res < 0:
            raise Exception("Failed to detach BPF from uprobe")
        self._del_uprobe(ev_name)

    def attach_uretprobe(self, name="", sym="", addr=None,
            fn_name="", pid=-1, cpu=0, group_fd=-1):
        """attach_uretprobe(name="", sym="", addr=None, fn_name=""
                            pid=-1, cpu=0, group_fd=-1)

        Run the bpf function denoted by fn_name every time the symbol sym in
        the library or binary 'name' finishes execution. See attach_uprobe for
        meaning of additional parameters.
        """

        (path, addr) = BPF._check_path_symbol(name, sym, addr)

        self._check_probe_quota(1)
        fn = self.load_func(fn_name, BPF.KPROBE)
        ev_name = "r_%s_0x%x" % (self._probe_repl.sub("_", path), addr)
        desc = "r:uprobes/%s %s:0x%x" % (ev_name, path, addr)
        res = lib.bpf_attach_uprobe(fn.fd, ev_name.encode("ascii"),
                desc.encode("ascii"), pid, cpu, group_fd,
                self._reader_cb_impl, ct.cast(id(self), ct.py_object))
        res = ct.cast(res, ct.c_void_p)
        if not res:
            raise Exception("Failed to attach BPF to uprobe")
        self._add_uprobe(ev_name, res)
        return self

    def detach_uretprobe(self, name="", sym="", addr=None):
        """detach_uretprobe(name="", sym="", addr=None)

        Stop running a bpf function that is attached to symbol 'sym' in library
        or binary 'name'.
        """

        (path, addr) = BPF._check_path_symbol(name, sym, addr)
        ev_name = "r_%s_0x%x" % (self._probe_repl.sub("_", path), addr)
        if ev_name not in self.open_uprobes:
            raise Exception("Kretprobe %s is not attached" % event)
        lib.perf_reader_free(self.open_uprobes[ev_name])
        desc = "-:uprobes/%s" % ev_name
        res = lib.bpf_detach_uprobe(desc.encode("ascii"))
        if res < 0:
            raise Exception("Failed to detach BPF from uprobe")
        self._del_uprobe(ev_name)

    def _trace_autoload(self):
        for i in range(0, lib.bpf_num_functions(self.module)):
            func_name = str(lib.bpf_function_name(self.module, i).decode())
            if func_name.startswith("kprobe__"):
                fn = self.load_func(func_name, BPF.KPROBE)
                self.attach_kprobe(event=fn.name[8:], fn_name=fn.name)
            elif func_name.startswith("kretprobe__"):
                fn = self.load_func(func_name, BPF.KPROBE)
                self.attach_kretprobe(event=fn.name[11:], fn_name=fn.name)
            elif func_name.startswith("tracepoint__"):
                fn = self.load_func(func_name, BPF.TRACEPOINT)
                tp = fn.name[len("tracepoint__"):].replace("__", ":")
                self.attach_tracepoint(tp=tp, fn_name=fn.name)

    def trace_open(self, nonblocking=False):
        """trace_open(nonblocking=False)

        Open the trace_pipe if not already open
        """
        if not self.tracefile:
            self.tracefile = open("%s/trace_pipe" % TRACEFS)
            if nonblocking:
                fd = self.tracefile.fileno()
                fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        return self.tracefile

    def trace_fields(self, nonblocking=False):
        """trace_fields(nonblocking=False)

        Read from the kernel debug trace pipe and return a tuple of the
        fields (task, pid, cpu, flags, timestamp, msg) or None if no
        line was read (nonblocking=True)
        """
        try:
            while True:
                line = self.trace_readline(nonblocking)
                if not line and nonblocking: return (None,) * 6
                # don't print messages related to lost events
                if line.startswith("CPU:"): continue
                task = line[:16].lstrip()
                line = line[17:]
                ts_end = line.find(":")
                pid, cpu, flags, ts = line[:ts_end].split()
                cpu = cpu[1:-1]
                msg = line[ts_end + 4:]
                return (task, int(pid), int(cpu), flags, float(ts), msg)
        except KeyboardInterrupt:
            exit()

    def trace_readline(self, nonblocking=False):
        """trace_readline(nonblocking=False)

        Read from the kernel debug trace pipe and return one line
        If nonblocking is False, this will block until ctrl-C is pressed.
        """

        trace = self.trace_open(nonblocking)

        line = None
        try:
            line = trace.readline(1024).rstrip()
        except IOError:
            pass
        except KeyboardInterrupt:
            exit()
        return line

    def trace_print(self, fmt=None):
        """trace_print(self, fmt=None)

        Read from the kernel debug trace pipe and print on stdout.
        If fmt is specified, apply as a format string to the output. See
        trace_fields for the members of the tuple
        example: trace_print(fmt="pid {1}, msg = {5}")
        """

        try:
            while True:
                if fmt:
                    fields = self.trace_fields(nonblocking=False)
                    if not fields: continue
                    line = fmt.format(*fields)
                else:
                    line = self.trace_readline(nonblocking=False)
                print(line)
                sys.stdout.flush()
        except KeyboardInterrupt:
            exit()

    @staticmethod
    def _sym_cache(pid):
        """_sym_cache(pid)

        Returns a symbol cache for the specified PID.
        The kernel symbol cache is accessed by providing any PID less than zero.
        """
        if pid < 0 and pid != -1:
            pid = -1
        if not pid in BPF._sym_caches:
            BPF._sym_caches[pid] = SymbolCache(pid)
        return BPF._sym_caches[pid]

    @staticmethod
    def sym(addr, pid):
        """sym(addr, pid)

        Translate a memory address into a function name for a pid, which is
        returned.
        A pid of less than zero will access the kernel symbol cache.
        """
        name, _ = BPF._sym_cache(pid).resolve(addr)
        return name

    @staticmethod
    def ksym(addr):
        """ksym(addr)

        Translate a kernel memory address into a kernel function name, which is
        returned.
        """
        return BPF.sym(addr, -1)

    @staticmethod
    def ksymaddr(addr):
        """ksymaddr(addr)

        Translate a kernel memory address into a kernel function name plus the
        instruction offset as a hexidecimal number, which is returned as a
        string.
        """
        name, offset = BPF._sym_cache(-1).resolve(addr)
        return "%s+0x%x" % (name, offset)

    @staticmethod
    def ksymname(name):
        """ksymname(name)

        Translate a kernel name into an address. This is the reverse of
        ksymaddr. Returns -1 when the function name is unknown."""
        return BPF._sym_cache(-1).resolve_name(name)

    def num_open_kprobes(self):
        """num_open_kprobes()

        Get the number of open K[ret]probes. Can be useful for scenarios where
        event_re is used while attaching and detaching probes. Excludes
        perf_events readers.
        """
        return len([k for k in self.open_kprobes.keys() if isinstance(k, str)])

    def kprobe_poll(self, timeout = -1):
        """kprobe_poll(self)

        Poll from the ring buffers for all of the open kprobes, calling the
        cb() that was given in the BPF constructor for each entry.
        """
        try:
            readers = (ct.c_void_p * len(self.open_kprobes))()
            for i, v in enumerate(self.open_kprobes.values()):
                readers[i] = v
            lib.perf_reader_poll(len(self.open_kprobes), readers, timeout)
        except KeyboardInterrupt:
            exit()

    def cleanup(self):
        for k, v in list(self.open_kprobes.items()):
            lib.perf_reader_free(v)
            # non-string keys here include the perf_events reader
            if isinstance(k, str):
                desc = "-:kprobes/%s" % k
                lib.bpf_detach_kprobe(desc.encode("ascii"))
            self._del_kprobe(k)
        for k, v in list(self.open_uprobes.items()):
            lib.perf_reader_free(v)
            desc = "-:uprobes/%s" % k
            lib.bpf_detach_uprobe(desc.encode("ascii"))
            self._del_uprobe(k)
        for k, v in self.open_tracepoints.items():
            lib.perf_reader_free(v)
            (tp_category, tp_name) = k.split(':')
            lib.bpf_detach_tracepoint(tp_category, tp_name)
        self.open_tracepoints.clear()
        if self.tracefile:
            self.tracefile.close()


from .usdt import USDT
