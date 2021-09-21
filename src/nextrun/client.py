import pickle
from typing import List

import grpc
import grpc.aio

from nextrun.config import DEFAULT_ADDRESS
from nextrun.deployed_function import NextRunDeployedFunction
from nextrun.nextrun_pb2 import (
    ProcessStatesRequest,
    RunPyFuncRequest,
    ProcessState,
    ServerAvailableFolder,
    GitRepoCommit,
)
from nextrun.nextrun_pb2_grpc import NextRunServerStub

# make this enum available for users
ProcessStateEnum = ProcessState.ProcessStateEnum


def _create_run_py_func_request(
    request_id: str, deployed_function: NextRunDeployedFunction
) -> RunPyFuncRequest:
    # first pickle the function arguments from job_run_spec

    # TODO add support for compressions, pickletools.optimize, possibly cloudpickle?

    # TODO also add the ability to write this to a shared location so that we don't need
    #  to pass it through the server.

    # TODO just hard-coding the interpreter version for now, need to actually grab it
    #  from the job_run_spec somehow
    interpreter_version = (3, 8, 0)

    # based on documentation in
    # https://docs.python.org/3/library/pickle.html#data-stream-format
    if interpreter_version >= (3, 8, 0):
        protocol = 5
    elif interpreter_version >= (3, 4, 0):
        protocol = 4
    elif interpreter_version >= (3, 0, 0):
        protocol = 3
    else:
        # TODO support for python 2 would require dealing with the string/bytes issue
        raise NotImplementedError("We currently only support python 3")

    protocol = min(protocol, pickle.HIGHEST_PROTOCOL)

    pickled_function_arguments = pickle.dumps(
        (
            deployed_function.next_run_function.function_args,
            deployed_function.next_run_function.function_kwargs,
        ),
        protocol=protocol,
    )

    # next, construct the RunPyFuncRequest

    result = RunPyFuncRequest(
        request_id=request_id,
        module_name=deployed_function.next_run_function.module_name,
        function_name=deployed_function.next_run_function.function_name,
        pickled_function_arguments=pickled_function_arguments,
        result_highest_pickle_protocol=pickle.HIGHEST_PROTOCOL,
    )

    # finally, add the deployment to the RunPyFuncRequest and return it

    if isinstance(deployed_function.deployment, ServerAvailableFolder):
        result.server_available_folder.CopyFrom(deployed_function.deployment)
    elif isinstance(deployed_function.deployment, GitRepoCommit):
        result.git_repo_commit.CopyFrom(deployed_function.deployment)
    else:
        raise ValueError(
            f"Unknown interpreter_and_code type {type(deployed_function.deployment)}"
        )

    return result


class NextRunClientAsync:
    """The main API for nextrun, allows callers to run functions on a nextrun server"""

    def __init__(self, address: str = DEFAULT_ADDRESS):
        self._channel = grpc.aio.insecure_channel(address)
        self._stub = NextRunServerStub(self._channel)

    async def run_py_func(
        self, request_id: str, deployed_function: NextRunDeployedFunction
    ) -> ProcessState:
        """
        Runs a function remotely on the NextRunServer.

        Explanation of JobRunSpecDeployedFunction fields:
        - The server will use the specified interpreter_path.
        - interpreter_version will be used to determine what pickle protocol can be used
          to send data to the remote function.
        - The server will set code_paths as the PYTHONPATH for the remote process and
          code_paths[0] as the working directory. The code_paths must "make sense" on
          the machine that NextRunServer is running on, NOT the current machine.
          code_paths must have at least one path. Order matters as usual for PYTHONPATH.
        - Within that PYTHONPATH, the NextRunServer will effectively try to do from
          [module_name] import [function_name]. Then it will effectively call
          function_name(*function_args, **function_kwargs). module_name can have dots
          like outer_package.inner_package.module as usual.

        Return value will includes a state (RUNNING or DUPLICATE_REQUEST_ID), and a pid
        for the remote process.

        Implementation notes:

        request_id uniquely identifies this request to avoid duplicates and for getting
        the results later. Make sure request_id is unique! Multiple requests with the
        same request_id will be treated as duplicates even if all of the other
        parameters are different. Also, request_id may only use string.ascii_letters,
        numbers, -, and _.

        result_highest_pickle_protocol tells the remote code what the highest pickle
        protocol we can read on this end is which will help it determine what pickle
        protocol to use to send back results.

        TODO return more information here, e.g. log file(s)

        TODO consider adding the ability for the client to get a callback/push
         notification?
        """
        return await self._stub.run_py_func(
            _create_run_py_func_request(request_id, deployed_function)
        )

    async def get_process_states(self, request_ids: List[str]) -> List[ProcessState]:
        """
        Gets the states and/or results for the processes corresponding to the specified
        request_ids. Will return one ProcessState for each request_id

        For each ProcessState:

        result.state will be one of ProcessStateEnum. Other fields will be populated
        depending on the ProcessStateEnum

        ProcessStateEnum values:
        - DEFAULT: reserved, not used
        - UNKNOWN: We don't recognize the request_id, no other fields will be populated
        - ERROR_GETTING_STATE: We do recognize the request_id, but there was an error
          getting the state of the process
        - SUCCEEDED: Completed normally. pickled_result, pid, will be populated
        - PYTHON_EXCEPTION: A python exception was thrown. pickled_result, pid will be
          populated. pickled_result will be a pickled tuple (exception_type,
          exception_message, exception_traceback). We don't pickle the exception itself
          because it may not be unpicklable on this end (e.g. it involves types that
          don't exist in the current process' code base). Obviously function_arguments
          and the function's results could be unpicklable as well, but those objects
          will hopefully be designed to be picklable/unpicklable, whereas exceptions are
          by their nature unexpected.
        - NON_ZERO_RETURN_CODE: The process exited with non-zero error code, which means
          that a non-python exception was thrown, or some python code called os.exit()
          with a non-zero argument. pid and return_code will be populated
        - CANCELLED: Cancelled by request.
        - RUNNING: Currently running. Only pid will be populated

        TODO add the ability to send results back to a shared location so that we don't
         need to pass through the results through the server
        """
        if not request_ids:
            return []
        if isinstance(request_ids, str):
            raise ValueError(
                "Must provide a list of request_ids, not just one request_id"
            )
        return (
            await self._stub.get_process_states(
                ProcessStatesRequest(request_ids=request_ids)
            )
        ).process_states

    async def __aenter__(self):
        await self._channel.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self._channel.__aexit__(exc_type, exc_val, exc_tb)


class NextRunClientSync:
    """The non-async version of NextRunClientAsync"""

    def __init__(self, address: str = DEFAULT_ADDRESS):
        self._channel = grpc.insecure_channel(address)
        self._stub = NextRunServerStub(self._channel)

    def run_py_func(
        self, request_id: str, deployed_function: NextRunDeployedFunction
    ) -> ProcessState:
        """See docstring on NextRunClientAsync"""
        return self._stub.run_py_func(
            _create_run_py_func_request(request_id, deployed_function)
        )

    def get_process_states(self, request_ids: List[str]) -> List[ProcessState]:
        """See docstring on NextRunClientAsync version"""
        if not request_ids:
            return []
        if isinstance(request_ids, str):
            raise ValueError(
                "Must provide a list of request_ids, not just one request_id"
            )
        return self._stub.get_process_states(
            ProcessStatesRequest(request_ids=request_ids)
        ).process_states

    def __enter__(self):
        self._channel.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._channel.__exit__(exc_type, exc_val, exc_tb)
