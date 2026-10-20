# Documentation

In-depth documentation for the A2A movie-night demo. For installation and a quick start, see the [main README](../README.md).

| Document | What it covers |
|----------|----------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components and ports, startup and agent discovery, the end-to-end request flow with a sequence diagram, sessions and `context_id` / `task_id`, error handling. |
| [A2A_PROTOCOL.md](A2A_PROTOCOL.md) | A2A concepts as used here (agent cards, skills, messages, parts, tasks, artifacts), real JSON-RPC `SendMessage` request/response examples, and the a2a-sdk 0.x to 1.x migration. |
| [MOVIE_AGENT_AND_CLICKHOUSE.md](MOVIE_AGENT_AND_CLICKHOUSE.md) | The IMDB dataset and its quirks, each of the six SQL tools explained, ranking strategy, parameter binding, performance, using your own ClickHouse, and the Neo4j-to-ClickHouse mapping. |
| [HOST_AND_SUSHMITA_AGENTS.md](HOST_AND_SUSHMITA_AGENTS.md) | The ADK Host Agent (discovery by `adk web`, dynamic instruction, `send_message`, movie plan tools) and the CrewAI Sushmita Agent (data, tools, crew, why it runs in a thread). |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Project layout, the `Problem/` exercise with every TODO, testing scripts, adding a tool or a new remote agent, notebooks, upgrading dependencies, Windows notes. |
| [CONFIGURATION.md](CONFIGURATION.md) | Every environment variable with its default and where it is read, `.env` vs `.env.example`, switching between the playground, ClickHouse Cloud and a local server, and model selection. |

Versions documented: google-adk 2.12, a2a-sdk 1.2.3 (A2A protocol 1.0), crewai 1.15, clickhouse-connect 1.11, Python 3.11 to 3.13.
