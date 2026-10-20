"""A2A executor that connects the Sushmita Agent's CrewAI crew to the a2a-sdk server.

``__main__.py`` passes a ``SchedulingAgentExecutor`` to ``DefaultRequestHandler``. For each JSON-RPC
``SendMessage`` request, the handler calls ``execute`` with a ``RequestContext`` (the user's message and
the task and context ids) and an ``EventQueue``. The events the executor puts on that queue become the
``Task`` that the caller (the Host Agent) receives.

Task lifecycle produced here
    1. Enqueue a new ``Task``, unless the request continues an existing one.
    2. ``start_work()``: state WORKING.
    3. Run the crew in a worker thread.
    4. ``add_artifact()`` with the answer text, then ``complete()``: state COMPLETED.
       If the crew raises, ``failed()`` with the error text instead: state FAILED.

Environment variables: none read here; ``SchedulingAgent`` requires ``GOOGLE_API_KEY``.
"""

import asyncio

from a2a.helpers import new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    InvalidParamsError,
    UnsupportedOperationError,
)
from agent import SchedulingAgent


class SchedulingAgentExecutor(AgentExecutor):
    """AgentExecutor for the scheduling and movie preferences agent.

    Wraps a single ``SchedulingAgent`` and turns each A2A request into one crew run.
    """

    def __init__(self):
        """Initializes the SchedulingAgentExecutor."""
        # One SchedulingAgent (CrewAI LLM + agent) is created when the server starts and reused for every
        # request. It raises ValueError if GOOGLE_API_KEY is missing.
        self.agent = SchedulingAgent()

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Executes the agent for scheduling and movie preferences queries.

        Args:
            context: The request: user message, task id, context id and the current task (if any).
            event_queue: Queue for the Task, status and artifact events that make up the response.
        """
        # The request handler fills these in; they identify the Task and its conversation.
        if not context.task_id or not context.context_id:
            raise ValueError("RequestContext must have task_id and context_id")
        if not context.message:
            raise ValueError("RequestContext must have a message")

        # a2a-sdk >= 1.0: a new Task must be enqueued before any status /
        # artifact update events (the old `updater.submit()` is not enough).
        # context.current_task is set only when the client continued an existing task (sent its task_id).
        if not context.current_task:
            await event_queue.enqueue_event(
                new_task_from_user_message(context.message)
            )
        # TaskUpdater enqueues status and artifact update events for this task.
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        # start_work(): the task moves to WORKING.
        await updater.start_work()

        # Validation hook; _validate_request currently accepts every request.
        if self._validate_request(context):
            # a2a-sdk >= 1.0: ServerError was removed; raise the A2A error directly.
            raise InvalidParamsError()

        # The text parts of the incoming message, joined into one string.
        query = context.get_user_input()
        try:
            # crewai >= 1.x refuses synchronous kickoff inside a running event loop,
            # so run the blocking crew in a worker thread. This also keeps the server's event loop free to
        # handle other requests while Gemini is working.
            result = await asyncio.to_thread(self.agent.invoke, query)
            print(f"Final Result ===> {result}")
        except Exception as e:
            print(f"Error invoking agent: {e}")
            # Report the failure on the task so callers see the cause (e.g. Gemini quota errors).
        # Raising instead would make the request handler return a generic JSON-RPC InternalError and
        # hide the real message. updater.failed() marks the task FAILED and puts the error text in
        # its status message.
            await updater.failed(
                message=updater.new_agent_message([new_text_part(f"Agent error: {e}")])
            )
            return

        # a2a-sdk >= 1.0: Part(root=TextPart(text=...)) -> Part(text=...)
        parts = [new_text_part(result)]

        # The answer becomes the task's artifact; the Host Agent reads artifacts[*].parts.
        await updater.add_artifact(parts)
        # COMPLETED is a final state; the handler then returns the finished Task to the caller.
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Handles task cancellation.

        Not supported: once started, the crew runs to completion.
        """
        raise UnsupportedOperationError()

    def _validate_request(self, context: RequestContext) -> bool:
        """Validates the request context.

        Returns:
            True if the request is invalid. This placeholder always returns False (accepts every request).
        """
        return False
