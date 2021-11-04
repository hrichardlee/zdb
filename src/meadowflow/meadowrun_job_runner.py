import pickle
import dataclasses
from typing import Iterable, Sequence, Union, Optional, Dict, Any

from meadowflow.event_log import Event, EventLog
from meadowflow.git_repo import GitRepo
from meadowflow.topic_names import TopicName
from meadowflow.jobs import (
    RaisedException,
    JobPayload,
    LocalFunction,
    JobRunnerFunction,
    JobRunner,
    VersionedJobRunnerFunction,
)
from meadowrun.client import MeadowRunClientAsync, ProcessStateEnum
from meadowrun.config import DEFAULT_ADDRESS
from meadowrun.deployed_function import (
    MeadowRunFunction,
    MeadowRunDeployedFunction,
    convert_local_to_deployed_function,
    MeadowRunDeployedCommand,
)


class MeadowRunJobRunner(JobRunner):
    """Integrates meadowrun with meadowflow. Runs jobs on a meadowrun server."""

    def __init__(self, event_log: EventLog, address: str = DEFAULT_ADDRESS):
        self._client = MeadowRunClientAsync(address)
        self._event_log = event_log

    async def _run_deployed_function(
        self,
        job_name: TopicName,
        run_request_id: str,
        deployed_function: Union[MeadowRunDeployedCommand, MeadowRunDeployedFunction],
    ) -> None:
        self._event_log.append_event(
            job_name, JobPayload(run_request_id, "RUN_REQUESTED")
        )

        if isinstance(deployed_function, MeadowRunDeployedCommand):
            result = await self._client.run_py_command(
                run_request_id, job_name.as_file_name(), deployed_function
            )
        elif isinstance(deployed_function, MeadowRunDeployedFunction):
            result = await self._client.run_py_func(
                run_request_id, job_name.as_file_name(), deployed_function
            )
        else:
            raise ValueError(
                f"Unexpected type of deployed_function {type(deployed_function)}"
            )

        if result.state == ProcessStateEnum.REQUEST_IS_DUPLICATE:
            # TODO handle this case and test it
            raise NotImplementedError()
        elif result.state == ProcessStateEnum.RUNNING:
            # TODO there is a very bad race condition here--the sequence of events could
            #  be:
            #  - run records RUN_REQUESTED
            #  - the meadowrun server runs the job and it completes
            #  - poll_jobs runs and records SUCCEEDED
            #  - the post-await continuation of run happens and records RUNNING
            self._event_log.append_event(
                job_name,
                JobPayload(run_request_id, "RUNNING", pid=result.pid),
            )
        elif result.state == ProcessStateEnum.RUN_REQUEST_FAILED:
            # TODO handle this case and test it
            raise NotImplementedError(str(pickle.loads(result.pickled_result)))
        else:
            raise ValueError(f"Did not expect ProcessStateEnum {result.state}")

    async def run(
        self,
        job_name: TopicName,
        run_request_id: str,
        job_runner_function: JobRunnerFunction,
    ) -> None:
        """Dispatches to _run_deployed_function which calls meadowrun"""
        if isinstance(
            job_runner_function, (MeadowRunDeployedCommand, MeadowRunDeployedFunction)
        ):
            await self._run_deployed_function(
                job_name, run_request_id, job_runner_function
            )
        elif isinstance(job_runner_function, LocalFunction):
            await self._run_deployed_function(
                job_name,
                run_request_id,
                convert_local_to_deployed_function(
                    job_runner_function.function_pointer,
                    job_runner_function.function_args,
                    job_runner_function.function_kwargs,
                ),
            )
        else:
            raise ValueError(
                f"job_runner_function of type {type(job_runner_function)} is not "
                "supported by MeadowRunJobRunner"
            )

    async def poll_jobs(self, last_events: Iterable[Event[JobPayload]]) -> None:
        """
        See docstring on base class. This code basically translates the meadowrun
        ProcessState into a JobPayload
        """

        last_events = list(last_events)
        process_states = await self._client.get_process_states(
            [e.payload.request_id for e in last_events]
        )

        if len(last_events) != len(process_states):
            raise ValueError(
                "get_process_states returned a different number of requests than "
                f"expected, sent {len(last_events)}, got back {len(process_states)} "
                "responses"
            )

        timestamp = self._event_log.curr_timestamp

        for last_event, process_state in zip(last_events, process_states):
            request_id = last_event.payload.request_id
            topic_name = last_event.topic_name
            if process_state.state == ProcessStateEnum.RUN_REQUESTED:
                # this should never actually get written because we should always be
                # creating a RUN_REQUESTED event in the run function before we poll
                new_payload = JobPayload(
                    request_id, "RUN_REQUESTED", pid=process_state.pid
                )
            elif process_state.state == ProcessStateEnum.RUNNING:
                new_payload = JobPayload(request_id, "RUNNING", pid=process_state.pid)
            elif process_state.state == ProcessStateEnum.SUCCEEDED:
                if process_state.pickled_result:
                    result_value, effects = pickle.loads(process_state.pickled_result)
                else:
                    result_value, effects = None, None

                new_payload = JobPayload(
                    request_id,
                    "SUCCEEDED",
                    pid=process_state.pid,
                    # TODO probably handle unpickling errors specially
                    result_value=result_value,
                    effects=effects,
                )
            elif process_state.state == ProcessStateEnum.RUN_REQUEST_FAILED:
                new_payload = JobPayload(
                    request_id,
                    "FAILED",
                    failure_type="RUN_REQUEST_FAILED",
                    raised_exception=RaisedException(
                        *pickle.loads(process_state.pickled_result)
                    ),
                )
            elif process_state.state == ProcessStateEnum.PYTHON_EXCEPTION:
                new_payload = JobPayload(
                    request_id,
                    "FAILED",
                    failure_type="PYTHON_EXCEPTION",
                    pid=process_state.pid,
                    # TODO probably handle unpickling errors specially
                    raised_exception=RaisedException(
                        *pickle.loads(process_state.pickled_result)
                    ),
                )
            elif process_state.state == ProcessStateEnum.NON_ZERO_RETURN_CODE:
                # TODO Test this case
                new_payload = JobPayload(
                    request_id,
                    "FAILED",
                    failure_type="NON_ZERO_RETURN_CODE",
                    pid=process_state.pid,
                    return_code=process_state.return_code,
                )
            elif process_state.state == ProcessStateEnum.CANCELLED:
                # TODO handle this and test it
                raise NotImplementedError("TBD")
            elif (
                process_state.state == ProcessStateEnum.UNKNOWN
                or process_state.state == ProcessStateEnum.ERROR_GETTING_STATE
            ):
                # TODO handle this case and test it
                raise NotImplementedError(
                    f"Not sure what to do here? Got {process_state.state} for job="
                    f"{topic_name} request_id={request_id}"
                )
            else:
                raise ValueError(
                    f"Did not expect ProcessStateEnum {process_state.state} for job="
                    f"{topic_name} request_id={request_id}"
                )

            # get the most recent updated_last_event. Because there's an await earlier
            # in this function, new events could have been added
            updated_last_event = self._event_log.last_event(topic_name, timestamp)

            if updated_last_event.payload.state != new_payload.state:
                if (
                    updated_last_event.payload.state == "RUN_REQUESTED"
                    and new_payload.state != "RUNNING"
                ):
                    self._event_log.append_event(
                        topic_name,
                        JobPayload(request_id, "RUNNING", pid=new_payload.pid),
                    )
                self._event_log.append_event(topic_name, new_payload)

    def can_run_function(self, job_runner_function: JobRunnerFunction) -> bool:
        return isinstance(
            job_runner_function,
            (MeadowRunDeployedCommand, MeadowRunDeployedFunction, LocalFunction),
        )

    async def __aenter__(self):
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self._client.__aexit__(exc_type, exc_val, exc_tb)


@dataclasses.dataclass(frozen=True)
class MeadowRunCommandGitRepo(VersionedJobRunnerFunction):
    """Represents a MeadowRunCommand in a git repo"""

    git_repo: GitRepo
    command_line: Sequence[str]
    context_variables: Optional[Dict[str, Any]] = None
    environment_variables: Optional[Dict[str, str]] = None

    def get_job_runner_function(self) -> MeadowRunDeployedCommand:
        return MeadowRunDeployedCommand(
            self.git_repo.get_commit(),
            self.command_line,
            self.context_variables,
            self.environment_variables,
        )


@dataclasses.dataclass(frozen=True)
class MeadowRunFunctionGitRepo(VersionedJobRunnerFunction):
    """Represents a MeadowRunFunction in a git repo"""

    git_repo: GitRepo
    meadowrun_function: MeadowRunFunction
    environment_variables: Optional[Dict[str, str]] = None

    def get_job_runner_function(self) -> MeadowRunDeployedFunction:
        return MeadowRunDeployedFunction(
            self.git_repo.get_commit(),
            self.meadowrun_function,
            self.environment_variables,
        )