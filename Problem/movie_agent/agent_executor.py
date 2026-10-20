"""Bridge between the A2A protocol server and the Movie Agent's Google ADK runner.

Role in the 3-agent system
--------------------------
The Host Agent sends the Movie Agent an A2A ``SendMessage`` JSON-RPC request. The a2a-sdk server
built in ``__main__.py`` decodes it and calls ``MovieAgentExecutor.execute()`` with a
``RequestContext`` (the incoming message plus task / context ids) and an ``EventQueue``. This
executor feeds the message to the ADK ``Runner`` (Gemini + the ClickHouse tools from
``agent.py``) and translates the ADK event stream back into A2A task events, which the SDK
returns to the caller (or streams, for ``SendStreamingMessage``).

A2A task lifecycle produced by ``execute()``
--------------------------------------------
1. **Task enqueued**: for a new conversation turn a ``Task`` object (state SUBMITTED, holding the
   user's message) is put on the queue first. a2a-sdk >= 1.0 requires the Task to exist before
   any status or artifact update refers to it.
2. **start_work()**: status -> ``TASK_STATE_WORKING``.
3. **Working updates**: intermediate model text (not tool calls) is sent as WORKING status
   messages, so streaming clients can show progress.
4. **Artifact**: the final model response is attached to the task as an artifact; this is the
   answer the Host Agent reads.
5. **complete()** -> ``TASK_STATE_COMPLETED``, or **failed()** -> ``TASK_STATE_FAILED`` with the
   error text if the model reported an error.

Sessions
--------
The A2A ``context_id`` (stable across all messages of one conversation) is used as the ADK
session id, so follow-up questions ("what about from the 90s?") see the earlier turns. Sessions
live in the runner's ``InMemorySessionService`` and are lost when the server restarts.

Part conversion
---------------
A2A and Gemini each have their own ``Part`` type. a2a-sdk 1.x uses a flat protobuf ``Part`` with
a ``content`` oneof (``text`` / ``raw`` bytes / ``url`` / ``data``); google.genai uses
``types.Part`` with ``text`` / ``inline_data`` (Blob) / ``file_data`` (URI). The module-level
``convert_*`` functions map between them.

This file is identical in ``Solution/`` and ``Problem/`` (it has no exercise gaps).
"""
import asyncio
import logging
from collections.abc import AsyncGenerator

from a2a.helpers import new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    Part,
    TaskState,
    UnsupportedOperationError,
)
from google.adk import Runner
from google.adk.events import Event
from google.genai import types

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class MovieAgentExecutor(AgentExecutor):
    """An AgentExecutor that runs the Movie Recommendation ADK-based Agent.

    The a2a-sdk ``DefaultRequestHandler`` calls ``execute()`` once per incoming message and
    ``cancel()`` for cancel requests. All results are reported by publishing events on the
    ``EventQueue`` (through a ``TaskUpdater``); nothing is returned directly.
    """

    def __init__(self, runner: Runner):
        """Stores the ADK runner (agent + session/artifact/memory services) built in __main__.py."""
        self.runner = runner
        # Not used yet; reserved for tracking in-flight sessions (e.g. to support cancellation).
        self._running_sessions = {}

    def _run_agent(
        self, session_id, new_message: types.Content
    ) -> AsyncGenerator[Event, None]:
        """Starts one agent turn and returns ADK's async event stream for it.

        ``Runner.run_async`` appends ``new_message`` to the session, calls Gemini, executes any
        tool calls the model makes, calls Gemini again with the tool results, and so on, yielding
        an ``Event`` for each step (model text, function call, function response, final answer).
        Every A2A caller shares the fixed ADK user id "movie_agent"; conversations are told apart
        by the session id.
        """
        return self.runner.run_async(
            session_id=session_id, user_id="movie_agent", new_message=new_message
        )

    async def _process_request(
        self,
        new_message: types.Content,
        session_id: str,
        task_updater: TaskUpdater,
    ) -> None:
        """Runs the agent on ``new_message`` and mirrors its events onto the A2A task.

        Args:
            new_message: The user's message, already converted to google.genai content.
            session_id: The ADK session to use (the A2A context id).
            task_updater: Helper that publishes status / artifact events for this task.
        """
        # Reuse the conversation's session (or create it on the first turn).
        session_obj = await self._upsert_session(session_id)
        session_id = session_obj.id

        async for event in self._run_agent(session_id, new_message):
            # google-adk >= 2.0 reports model errors (quota, auth, ...) as events instead of raising.
            # Such an event carries error_code / error_message and no useful content, so without
            # this check the loop would end without ever completing the task. Report it as
            # TASK_STATE_FAILED with the error text so the caller sees why (e.g. 429 quota).
            if event.error_code or event.error_message:
                logger.error("Agent error %s: %s", event.error_code, event.error_message)
                await task_updater.failed(
                    message=task_updater.new_agent_message(
                        [Part(text=f"Agent error {event.error_code}: {event.error_message}")]
                    )
                )
                break
            # The final response is the model's answer after all tool calls are done: publish it
            # as the task's artifact, then mark the task completed (a terminal state).
            if event.is_final_response():
                parts = convert_genai_parts_to_a2a(
                    event.content.parts if event.content and event.content.parts else []
                )
                logger.debug("Yielding final response: %s", parts)
                await task_updater.add_artifact(parts)
                await task_updater.complete()
                break
            # Intermediate events: forward model text as a WORKING status update. Function-call
            # events (the model asking to run a tool) are internal plumbing and are not sent.
            # Function-response events have no text/file parts, so they become empty updates.
            if not event.get_function_calls():
                logger.debug("Yielding update response")
                await task_updater.update_status(
                    TaskState.TASK_STATE_WORKING,
                    message=task_updater.new_agent_message(
                        convert_genai_parts_to_a2a(
                            event.content.parts
                            if event.content and event.content.parts
                            else []
                        ),
                    ),
                )
            else:
                logger.debug("Skipping event")

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ):
        """Handles one A2A ``SendMessage`` / ``SendStreamingMessage`` request.

        Args:
            context: The request: the user's message, its task id and context (conversation) id,
                and the existing task if this message continues one.
            event_queue: Where task, status and artifact events are published; the request
                handler turns them into the JSON-RPC response or stream.

        Raises:
            ValueError: if the request has no task id, context id or message.
        """
        # The request handler assigns both ids before calling execute(); guard anyway.
        if not context.task_id or not context.context_id:
            raise ValueError("RequestContext must have task_id and context_id")
        if not context.message:
            raise ValueError("RequestContext must have a message")

        # a2a-sdk >= 1.0: a new Task must be enqueued before any status /
        # artifact update events (the old `updater.submit()` is not enough).
        if not context.current_task:
            await event_queue.enqueue_event(
                new_task_from_user_message(context.message)
            )
        # TaskUpdater stamps every event it publishes with this task id and context id.
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        # Convert the A2A message to Gemini content and run the agent. The context id doubles as
        # the ADK session id so the conversation's history is kept between messages.
        await self._process_request(
            types.UserContent(
                parts=convert_a2a_parts_to_genai(context.message.parts),
            ),
            context.context_id,
            updater,
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        """Cancellation is not supported: a running ADK turn cannot be interrupted here.

        Raises:
            UnsupportedOperationError: always; the SDK returns it to the client as an A2A error.
        """
        # a2a-sdk >= 1.0: ServerError was removed; raise the A2A error directly.
        raise UnsupportedOperationError()

    async def _upsert_session(self, session_id: str):
        """Returns the ADK session with this id, creating it if it does not exist yet.

        Args:
            session_id: The session id to look up (the A2A context id).

        Raises:
            RuntimeError: if the session can neither be found nor created.
        """
        session = await self.runner.session_service.get_session(
            app_name=self.runner.app_name, user_id="movie_agent", session_id=session_id
        )
        if session is None:
            # First message of this conversation: create the session under the same id so later
            # messages with the same context id find it.
            session = await self.runner.session_service.create_session(
                app_name=self.runner.app_name,
                user_id="movie_agent",
                session_id=session_id,
            )
        if session is None:
            raise RuntimeError(f"Failed to get or create session: {session_id}")
        return session


def convert_a2a_parts_to_genai(parts: list[Part]) -> list[types.Part]:
    """Convert a list of A2A Part types into a list of Google Gen AI Part types."""
    return [convert_a2a_part_to_genai(part) for part in parts]


def convert_a2a_part_to_genai(part: Part) -> types.Part:
    """Convert a single A2A Part type into a Google Gen AI Part type.

    a2a-sdk >= 1.0 uses a flat protobuf `Part` with a `content` oneof
    (text / raw / url / data) instead of TextPart / FilePart wrappers.

    Mapping: ``text`` -> ``Part(text=...)``; ``url`` -> ``file_data`` (a file reference by URI);
    ``raw`` bytes -> ``inline_data`` (a Blob). Structured ``data`` parts are not supported.

    Raises:
        ValueError: for a ``data`` part or an empty part.
    """
    # WhichOneof returns the name of the field set in the `content` oneof (or None if empty).
    content = part.WhichOneof("content")
    if content == "text":
        return types.Part(text=part.text)
    if content == "url":
        return types.Part(
            file_data=types.FileData(
                # Protobuf strings default to "", so map an empty media type to None (unknown).
                file_uri=part.url, mime_type=part.media_type or None
            )
        )
    if content == "raw":
        return types.Part(
            inline_data=types.Blob(
                data=part.raw,
                mime_type=part.media_type or "application/octet-stream",
            )
        )
    raise ValueError(f"Unsupported part type: {content}")


def convert_genai_parts_to_a2a(parts: list[types.Part]) -> list[Part]:
    """Convert a list of Google Gen AI Part types into a list of A2A Part types.

    Parts with no text, file or inline data (e.g. function calls/responses, thought signatures)
    are dropped, since they have no A2A equivalent and are internal to the agent.
    """
    return [
        convert_genai_part_to_a2a(part)
        for part in parts
        if (part.text or part.file_data or part.inline_data)
    ]


def convert_genai_part_to_a2a(part: types.Part) -> Part:
    """Convert a single Google Gen AI Part type into an A2A Part type.

    Mapping: ``text`` -> ``Part(text=...)``; ``file_data`` -> ``Part(url=..., media_type=...)``;
    ``inline_data`` -> ``Part(raw=..., media_type=...)``.

    Raises:
        ValueError: if a file part has no URI / data, or the part has no supported content.
    """
    if part.text:
        return Part(text=part.text)
    if part.file_data:
        if not part.file_data.file_uri:
            raise ValueError("File URI is missing")
        return Part(
            url=part.file_data.file_uri,
            media_type=part.file_data.mime_type,
        )
    if part.inline_data:
        if not part.inline_data.data:
            raise ValueError("Inline data is missing")
        # Protobuf carries raw bytes directly; no base64/utf-8 round-trip needed.
        return Part(
            raw=part.inline_data.data,
            media_type=part.inline_data.mime_type,
        )
    raise ValueError(f"Unsupported part type: {part}")
