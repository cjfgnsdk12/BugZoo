from typing import List, Dict, Optional, Set
import tempfile
import os
import warnings
import xml.etree.ElementTree as ET

from ..core.fileline import FileLineSet
from ..core.container import Container
from ..core.coverage import TestSuiteCoverage, \
                            TestCoverage
from ..testing.base import TestCase


class CoverageManager(object):
    INSTRUMENTATION = (
        "// BUGZOO :: INSTRUMENTATION :: START\n"
        "#include <stdio.h>\n"
        "#include <stdlib.h>\n"
        "#include <signal.h>\n"
        "void bugzoo_sighandler(int sig){\n"
        "  exit(1);\n"
        "}\n"
        "void bugzoo_ctor (void) __attribute__ ((constructor));\n"
        "void bugzoo_ctor (void) {\n"
        "  struct sigaction new_action;\n"
        "  new_action.sa_handler = bugzoo_sighandler;\n"
        "  sigemptyset(&new_action.sa_mask);\n"
        "  new_action.sa_flags = 0;\n"
        "  sigaction(SIGTERM, &new_action, NULL);\n"
        "  sigaction(SIGINT, &new_action, NULL);\n"
        "  sigaction(SIGKILL, &new_action, NULL);\n"
        "}\n"
        "// BUGZOO :: INSTRUMENTATION :: END\n"
    )

    def _from_gcovr_xml_string(self,
                               s: str,
                               instrumented_files: Set[str],
                               container: Container
                               ) -> FileLineSet:
        """
        Determines the set of files that are covered in a gcovr report.

        Parameters:
            s: a string-encoding of an XML document containing a gcovr report.
            instrumented_files: the paths (relative to the source code
                directory) to all files that were instrumented.

        Returns:
            the set of file-lines that are stated as covered by the given
            report.
        """
        logger = self.__logger.getChild(container.id)
        mgr_ctr = self.__installation.containers
        dir_source = container.bug.source_dir # TODO port

        def has_file(fn_rel: str) -> bool:
            fn_abs = os.path.join(dir_source, fn_rel)
            cmd = 'test -f "{}"'.format(fn_abs)
            resp = mgr_ctr.command(container, cmd)
            return resp.code == 0

        # FIXME is this a general solution?
        def resolve_path(fn_rel: str) -> str:
            assert fn_rel != '', "failed to resolve path"
            if has_file(fn_rel):
                return fn_rel

            fn_rel_child = '/'.join(fn_rel.split('/')[1:])
            return resolve_path(fn_rel_child)

        assert isinstance(instrumented_files, set)
        for path in instrumented_files:
            assert not os.path.isabs(path), "expected relative file paths"

        root = ET.fromstring(s)
        files_to_lines = {}
        packages = root.find('packages')

        for package in packages.findall('package'):
            for cls in package.find('classes').findall('class'):
                try:
                    path = resolve_path(cls.attrib['filename'])
                except AssertionError:
                    logger.warning("failed to resolve file: %s", path)
                    continue

                lines = cls.find('lines').findall('line')
                lines = set(int(l.attrib['number']) for l in lines \
                            if int(l.attrib['hits']) > 0)
                if lines != []:
                    files_to_lines[path] = lines

        # modify coverage information for all of the instrumented files
        num_instrumentation_lines = CoverageManager.INSTRUMENTATION.count('\n')
        lines_to_remove = set(range(1, num_instrumentation_lines))
        for path in instrumented_files:
            if not path in files_to_lines:
                continue

            logger.debug("Removing coverage lines due to instrumentation: {}".format(path))
            lines = files_to_lines[path]
            lines -= lines_to_remove
            tmp = set()
            for line in lines:
                tmp.add(line - num_instrumentation_lines)
            files_to_lines[path] = tmp

        return FileLineSet(files_to_lines)

    def __init__(self, installation: 'BugZoo'):
        self.__installation = installation
        self.__logger = installation.logger.getChild('coverage')

    def coverage(self,
                 container: Container,
                 tests: List[TestCase],
                 files_to_instrument: List[str] = None,
                 ) -> TestSuiteCoverage:
        """
        Uses a provided container to compute line coverage information for a
        given list of tests.
        """
        assert tests != []

        if files_to_instrument is None:
            files_to_instrument = []

        self.instrument(container,
                        files_to_instrument=files_to_instrument)

        cov = {}
        for test in tests:
            print("generating coverage: {}".format(test))
            outcome = self.__installation.containers.execute(container, test)
            filelines = self.extract(container,
                                     instrumented_files=files_to_instrument)
            cov[test.name] = TestCoverage(test.name, outcome, filelines)

        self.deinstrument(container,
                          instrumented_files=files_to_instrument)

        return TestSuiteCoverage(cov)

    def instrument(self,
                   container: Container,
                   files_to_instrument: Optional[List[str]] = None
                   ) -> None:
        """
        Adds source code instrumentation and recompiles the program inside
        a container using the appropriate GCC options. Also ensures that
        gcovr is installed inside the container.

        Parameters:
            container: the container whose contents should be instrumented.
            files_to_instrument: the paths to the source code files that
                should be instrumented (relative to the source code directory).

        Raises:
            Exception: if an absolute file path is provided.
        """
        mgr_ctr = self.__installation.containers

        if not files_to_instrument:
            files_to_instrument = []

        for path in files_to_instrument:
            assert not os.path.isabs(path), "expected relative file paths"

        # ensure that gcovr is mounted within the container
        # TODO: mount binaries
        mgr_ctr.command(container,
                        'sudo apt-get update && sudo apt-get install -y gcovr')

        # add instrumentation to each file
        dir_source = container.bug.source_dir # TODO port
        for fn_src in files_to_instrument:
            fn_src = os.path.join(dir_source, fn_src)
            (_, fn_temp) = tempfile.mkstemp(suffix='.bugzoo')
            try:
                mgr_ctr.copy_from(container, fn_src, fn_temp)
                with open(fn_temp, 'r') as fh:
                    contents = CoverageManager.INSTRUMENTATION + fh.read()

                with open(fn_temp, 'w') as fh:
                    fh.write(contents)
                mgr_ctr.copy_to(container, fn_temp, fn_src)
            finally:
                os.remove(fn_temp)

        # recompile with instrumentation options
        outcome = mgr_ctr.compile_with_instrumentation(container)
        if not outcome.successful:
            msg = "failed to generate coverage for container ({}) due to compilation failure."
            msg = msg.format(container.id)
            print(outcome.response.output)
            raise Exception(msg)

    def deinstrument(self,
                     container: Container,
                     instrumented_files: Optional[List[str]] = None
                     ) -> None:
        """
        Strips instrumentation from the source code inside a given container,
        and reconfigures its program to no longer use coverage options.
        """
        mgr = self.__installation.containers
        num_lines_to_remove = CoverageManager.INSTRUMENTATION.count('\n')

        # remove source code instrumentation
        for fn_instrumented in instrumented_files:
            cmd = "sed -i '1,{}d' '{}'"
            cmd.format(num_lines_to_remove, fn_instrumented)
            mgr.command(container, cmd)

        # TODO recompile with standard flags
        pass

    def extract(self,
                container: Container,
                instrumented_files: Optional[List[str]] = None
                ) -> FileLineSet:
        """
        Uses gcovr to extract coverage information for all of the C/C++ source
        code files within the project. Destroys '.gcda' files upon computing
        coverage.
        """
        mgr_ctr = self.__installation.containers

        if not instrumented_files:
            instrumented_files = set()
        else:
            instrumented_files = set(instrumented_files)

        dir_source = container.bug.source_dir # TODO port
        response = mgr_ctr.command(container,
                                   'gcovr -x -d -r .',
                                   context=dir_source)
        assert response.code == 0
        response = response.output

        return self._from_gcovr_xml_string(response,
                                           instrumented_files,
                                           container)