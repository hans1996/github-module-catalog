"""Precision and real-corpus regressions for the packaged Taxonomy v2."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from github_module_catalog.models import RepositoryObservation
from github_module_catalog.taxonomy import classify_repository, load_taxonomy

TAXONOMY_PATH = (
    Path(__file__).parents[2] / "src" / "github_module_catalog" / "data" / "taxonomy.yaml"
)


def repository_fixture(**overrides: object) -> RepositoryObservation:
    values: dict[str, Any] = {
        "identity": {"repository_id": 42},
        "owner": "octocat",
        "name": "toolkit",
        "full_name": "octocat/toolkit",
        "html_url": "https://github.com/octocat/toolkit",
        "description": "Authentication REST API and command-line toolkit",
        "topics": ["Auth", "CLI", "api"],
        "primary_language": "Python",
        "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2024, 2, 1, tzinfo=UTC),
        "pushed_at": None,
        "observed_at": datetime(2024, 2, 2, tzinfo=UTC),
        "archived": False,
        "disabled": False,
        "fork": False,
        "license_spdx": "MIT",
        "license_name": "MIT License",
    }
    return RepositoryObservation(**(values | overrides))


@pytest.mark.parametrize(
    ("topic", "forbidden_capability"),
    [
        ("terminal", "terminal-emulator"),
        ("docker", "container-tooling"),
        ("kubernetes", "kubernetes-tooling"),
        ("crypto", "cryptography"),
        ("monitoring", "metrics-monitoring"),
        ("tls", "cryptography"),
        ("malware", "malware-analysis"),
        ("sso", "identity-provider"),
        ("2fa", "multi-factor-auth"),
    ],
)
def test_packaged_v2_avoids_ambiguous_leaf_topics(
    topic: str,
    forbidden_capability: str,
) -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(description=None, topics=[topic], primary_language=None)

    capability_ids = {
        assertion.capability_id for assertion in classify_repository(observation, taxonomy)
    }

    assert forbidden_capability not in capability_ids


@pytest.mark.parametrize(
    ("protocol_topic", "forbidden_capability"),
    [
        ("grpc", "rpc-api"),
        ("json-rpc", "rpc-api"),
        ("socket-io", "realtime-api"),
        ("websocket", "realtime-api"),
        ("websockets", "realtime-api"),
    ],
)
def test_packaged_v2_does_not_treat_protocol_only_topics_as_server_capabilities(
    protocol_topic: str,
    forbidden_capability: str,
) -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(
        description=None,
        topics=[protocol_topic],
        primary_language=None,
    )

    capability_ids = {
        assertion.capability_id for assertion in classify_repository(observation, taxonomy)
    }

    assert forbidden_capability not in capability_ids


def test_packaged_v2_does_not_treat_protocol_only_descriptions_as_rpc_servers() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(
        description="A JSON-RPC protocol implementation and specification toolkit",
        topics=[],
        primary_language=None,
    )

    capability_ids = {
        assertion.capability_id for assertion in classify_repository(observation, taxonomy)
    }

    assert "rpc-api" not in capability_ids


def test_packaged_v2_recognizes_apache_dubbo_as_an_rpc_framework() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(
        owner="apache",
        name="dubbo",
        full_name="apache/dubbo",
        html_url="https://github.com/apache/dubbo",
        description="The java implementation of Apache Dubbo. An RPC and microservice framework.",
        topics=[
            "distributed-systems",
            "dubbo",
            "framework",
            "grpc",
            "http",
            "java",
            "microservices",
            "restful",
            "rpc",
            "service-mesh",
            "web",
        ],
        primary_language="Java",
    )

    capability_ids = {
        assertion.capability_id for assertion in classify_repository(observation, taxonomy)
    }

    assert {"api-backend", "rpc-api"}.issubset(capability_ids)


def test_packaged_v2_recognizes_socket_io_as_a_realtime_api() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(
        owner="socketio",
        name="socket.io",
        full_name="socketio/socket.io",
        html_url="https://github.com/socketio/socket.io",
        description="Bidirectional and low-latency communication for every platform",
        topics=["javascript", "nodejs", "socket-io", "websocket"],
        primary_language="TypeScript",
    )

    capability_ids = {
        assertion.capability_id for assertion in classify_repository(observation, taxonomy)
    }

    assert {"api-backend", "realtime-api"}.issubset(capability_ids)


@pytest.mark.parametrize(
    ("signal_topic", "noise_topic", "forbidden_capability"),
    [
        ("agent-framework", "awesome-list", "ai-agent-framework"),
        ("relational-database", "tutorials", "relational-database"),
        ("penetration-testing", "course", "penetration-testing"),
    ],
)
def test_packaged_v2_rejects_resource_only_projects(
    signal_topic: str,
    noise_topic: str,
    forbidden_capability: str,
) -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(
        description=None,
        topics=[signal_topic, noise_topic],
        primary_language=None,
    )

    capability_ids = {
        assertion.capability_id for assertion in classify_repository(observation, taxonomy)
    }

    assert forbidden_capability not in capability_ids


def test_precision_first_resource_veto_beats_strong_signal_with_only_tutorial_topic() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(
        description="A production-ready retrieval augmented generation engine",
        topics=["tutorial"],
        primary_language="Python",
    )

    assert classify_repository(observation, taxonomy) == ()


@pytest.mark.parametrize(
    ("name", "description", "topics", "forbidden_capabilities"),
    [
        (
            "postgres-app",
            "An application backed by PostgreSQL",
            ["postgresql"],
            ("relational-database",),
        ),
        (
            "mongo-cache-app",
            "An application using MongoDB and Redis",
            ["mongodb", "redis"],
            ("document-database", "cache-key-value"),
        ),
        (
            "mall",
            "A Spring Boot ecommerce application",
            ["elasticsearch", "mongodb", "mysql", "redis"],
            (
                "relational-database",
                "document-database",
                "cache-key-value",
                "search-engine",
            ),
        ),
        (
            "hackathon-starter",
            "A boilerplate for Node.js web applications",
            ["boilerplate", "oauth2", "starter-kit"],
            ("oauth-oidc",),
        ),
        (
            "hoppscotch",
            "An API client and alternative to Postman",
            ["api-client", "api-rest", "rest-api"],
            ("rest-api",),
        ),
        (
            "browser-use",
            "Automate tasks online with AI agents",
            ["browser-automation", "playwright"],
            ("browser-e2e-testing",),
        ),
        (
            "website-change-monitor",
            "Monitor news and website changes",
            ["monitoring"],
            ("metrics-monitoring",),
        ),
        (
            "tls-client",
            "An HTTPS client with TLS encryption",
            ["encryption", "tls"],
            ("cryptography",),
        ),
        (
            "awesome-llm-apps",
            "100 AI Agent and RAG apps you can run",
            ["rag"],
            ("rag-retrieval",),
        ),
        (
            "cs-video-courses",
            "List of Computer Science courses with video lectures",
            ["computer-vision"],
            ("computer-vision",),
        ),
        (
            "90DaysOfDevOps",
            "A structured learning map covering DevOps tools",
            ["ansible", "terraform"],
            ("configuration-management", "infrastructure-as-code"),
        ),
        (
            "tailscale",
            "The easiest secure way to use WireGuard and 2FA",
            ["2fa", "oauth", "sso", "vpn", "wireguard"],
            ("identity-provider", "multi-factor-auth"),
        ),
        (
            "trivy",
            "Find vulnerabilities and misconfigurations in code and clouds",
            ["infrastructure-as-code", "misconfiguration", "vulnerability-scanners"],
            ("infrastructure-as-code",),
        ),
        (
            "algo",
            "Set up a personal VPN in the cloud",
            ["ansible", "vpn", "vpn-server"],
            ("configuration-management",),
        ),
        (
            "hosts",
            "Consolidating hosts files that block malicious domains",
            ["malware", "security"],
            ("malware-analysis",),
        ),
        (
            "LLMs-from-scratch",
            "Implement a language model in PyTorch step by step",
            ["finetuning", "from-scratch", "llm"],
            ("model-training",),
        ),
        (
            "awesome-agent-frameworks",
            "Resources for agent builders",
            ["agent-framework"],
            ("ai-agent-framework",),
        ),
        (
            "ai-agents-for-beginners",
            "Lessons to get started building AI agents",
            ["agentic-framework"],
            ("ai-agent-framework",),
        ),
    ],
)
def test_packaged_v2_rejects_known_application_dependency_and_resource_false_positives(
    name: str,
    description: str,
    topics: list[str],
    forbidden_capabilities: tuple[str, ...],
) -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(
        owner="example",
        name=name,
        full_name=f"example/{name}",
        html_url=f"https://github.com/example/{name}",
        description=description,
        topics=topics,
        primary_language=None,
    )

    capability_ids = {
        assertion.capability_id for assertion in classify_repository(observation, taxonomy)
    }

    assert set(forbidden_capabilities).isdisjoint(capability_ids)


@pytest.mark.parametrize(
    ("full_name", "description", "topics", "primary_language", "forbidden_capabilities"),
    [
        (
            "Kong/insomnia",
            "The open-source, cross-platform API client for GraphQL, REST, WebSockets, SSE "
            "and gRPC. With Cloud, Local and Git storage.",
            [
                "api",
                "api-client",
                "api-design",
                "curl",
                "electron-app",
                "graphql",
                "grpc",
                "http-client",
                "rest-api",
                "websockets",
            ],
            "TypeScript",
            ("api-backend", "database-storage", "rpc-api"),
        ),
        (
            "curl/curl",
            "A command line tool and library for transferring data with URL syntax, supporting "
            "DICT, FILE, FTP, FTPS, GOPHER, GOPHERS, HTTP, HTTPS, IMAP, IMAPS, LDAP, LDAPS, "
            "MQTT, MQTTS, POP3, POP3S, RTSP, SCP, SFTP, SMB, SMBS, SMTP, SMTPS, TELNET, TFTP, "
            "WS and WSS. libcurl offers a myriad of powerful features",
            [
                "c",
                "client",
                "curl",
                "ftp",
                "gopher",
                "hacktoberfest",
                "http",
                "https",
                "imaps",
                "ldap",
                "libcurl",
                "library",
                "mqtt",
                "pop3",
                "scp",
                "sftp",
                "transfer-data",
                "transferring-data",
                "user-agent",
                "websocket",
            ],
            "C",
            ("realtime-api",),
        ),
        (
            "mitmproxy/mitmproxy",
            "An interactive TLS-capable intercepting HTTP proxy for penetration testers and "
            "software developers.",
            [
                "debugging",
                "http",
                "http2",
                "man-in-the-middle",
                "mitmproxy",
                "proxy",
                "python",
                "security",
                "ssl",
                "tls",
                "websocket",
            ],
            "Python",
            ("realtime-api",),
        ),
    ],
)
def test_packaged_v2_rejects_known_protocol_client_false_positives(
    full_name: str,
    description: str,
    topics: list[str],
    primary_language: str,
    forbidden_capabilities: tuple[str, ...],
) -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    owner, name = full_name.split("/", maxsplit=1)
    observation = repository_fixture(
        owner=owner,
        name=name,
        full_name=full_name,
        html_url=f"https://github.com/{full_name}",
        description=description,
        topics=topics,
        primary_language=primary_language,
    )

    capability_ids = {
        assertion.capability_id for assertion in classify_repository(observation, taxonomy)
    }

    assert set(forbidden_capabilities).isdisjoint(capability_ids)


def test_packaged_v2_rejects_prompt_engineering_guide_as_a_rag_module() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(
        owner="dair-ai",
        name="Prompt-Engineering-Guide",
        full_name="dair-ai/Prompt-Engineering-Guide",
        html_url="https://github.com/dair-ai/Prompt-Engineering-Guide",
        description=(
            "Guides, papers, lessons, notebooks and resources for prompt engineering, context "
            "engineering, RAG, and AI Agents."
        ),
        topics=[
            "agent",
            "agents",
            "ai-agents",
            "chatgpt",
            "deep-learning",
            "generative-ai",
            "language-model",
            "llms",
            "openai",
            "prompt-engineering",
            "rag",
        ],
        primary_language="MDX",
    )

    capability_ids = {
        assertion.capability_id for assertion in classify_repository(observation, taxonomy)
    }

    assert "rag-retrieval" not in capability_ids


def test_packaged_v2_rejects_ai_engineering_hub_tutorials_as_a_rag_module() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(
        owner="patchy631",
        name="ai-engineering-hub",
        full_name="patchy631/ai-engineering-hub",
        html_url="https://github.com/patchy631/ai-engineering-hub",
        description="In-depth tutorials on LLMs, RAGs and real-world AI agent applications.",
        topics=["agents", "ai", "llms", "machine-learning", "mcp", "rag"],
        primary_language=None,
    )

    capability_ids = {
        assertion.capability_id for assertion in classify_repository(observation, taxonomy)
    }

    assert "rag-retrieval" not in capability_ids


@pytest.mark.parametrize(
    ("full_name", "description", "topics", "primary_language"),
    [
        (
            "Snailclimb/JavaGuide",
            "Java 面试 & 后端通用面试指南，覆盖计算机基础、数据库、"
            "分布式、高并发、系统设计与 AI 应用开发",
            [
                "agent",
                "ai",
                "context-engineering",
                "deepseek",
                "interview",
                "java",
                "mcp",
                "mysql",
                "redis",
                "redisson",
                "skills",
                "springai",
                "system-design",
            ],
            "JavaScript",
        ),
        (
            "openai/openai-cookbook",
            "Examples and guides for using the OpenAI API",
            ["chatgpt", "gpt-4", "openai", "openai-api"],
            "Jupyter Notebook",
        ),
        (
            "thedaviddias/Front-End-Checklist",
            "🗂 The essential checklist for modern web development, for humans and AI agents",
            [
                "ai-agent",
                "ai-agents",
                "checklist",
                "css",
                "front-end-developer-tool",
                "front-end-development",
                "frontend",
                "guidelines",
                "html",
                "javascript",
                "lists",
                "reference",
                "resources",
                "rules",
                "web-development",
            ],
            "MDX",
        ),
        (
            "labmlai/annotated_deep_learning_paper_implementations",
            "🧑‍🏫 60+ Implementations/tutorials of deep learning papers with side-by-side notes "
            "📝; including transformers (original, xl, switch, feedback, vit, ...), optimizers "
            "(adam, adabelief, sophia, ...), gans(cyclegan, stylegan2, ...), 🎮 reinforcement "
            "learning (ppo, dqn), capsnet, distillation, ... 🧠",
            [
                "attention",
                "deep-learning",
                "deep-learning-tutorial",
                "gan",
                "literate-programming",
                "lora",
                "machine-learning",
                "neural-networks",
                "optimizers",
                "pytorch",
                "reinforcement-learning",
                "transformer",
                "transformers",
            ],
            "Python",
        ),
        (
            "SimplifyJobs/Summer2026-Internships",
            "Summer 2026 software engineering, data science, AI, quant, product management, "
            "and hardware internship postings. Updated daily by Simplify and Pitt CSC.",
            [
                "data-science",
                "fall-2026",
                "github",
                "internship",
                "internships",
                "interview-preparation",
                "jobs",
                "software-engineering",
                "university",
            ],
            "Python",
        ),
        (
            "fengdu78/Coursera-ML-AndrewNg-Notes",
            "吴恩达老师的机器学习课程个人笔记",
            ["coursera", "machine-learning"],
            "HTML",
        ),
        (
            "0voice/interview_internal_reference",
            "2025年最新总结，阿里，腾讯，百度，美团，头条等技术面试题目，以及答案，专家出题人分析汇总。",
            [
                "cpu",
                "high-performance",
                "interview",
                "mongodb",
                "mysql",
                "network",
                "nginx",
                "redis",
                "storage",
                "zookeeper",
            ],
            "Python",
        ),
    ],
)
def test_packaged_v2_rejects_known_resource_only_corpus(
    full_name: str,
    description: str,
    topics: list[str],
    primary_language: str,
) -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    owner, name = full_name.split("/", maxsplit=1)
    observation = repository_fixture(
        owner=owner,
        name=name,
        full_name=full_name,
        html_url=f"https://github.com/{full_name}",
        description=description,
        topics=topics,
        primary_language=primary_language,
    )

    assert classify_repository(observation, taxonomy) == ()


def test_resource_filter_does_not_veto_a_real_tool_that_mentions_learning_material() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(
        description="An awesome package manager with a tutorial for new users",
        topics=["package-manager"],
        primary_language=None,
    )

    capability_ids = {
        assertion.capability_id for assertion in classify_repository(observation, taxonomy)
    }

    assert "package-manager" in capability_ids


@pytest.mark.parametrize(
    ("name", "description", "topics", "expected_capability"),
    [
        (
            "keycloak",
            "Open Source Identity and Access Management for modern applications",
            ["oidc", "saml"],
            "identity-provider",
        ),
        (
            "ansible",
            "An IT automation platform for deployment and network configuration",
            ["ansible"],
            "configuration-management",
        ),
        (
            "terraform",
            "A tool that codifies infrastructure into declarative configuration",
            ["infrastructure-as-code", "terraform"],
            "infrastructure-as-code",
        ),
        (
            "dify",
            "Production-ready platform for agentic workflow development.",
            [
                "agent",
                "agentic-ai",
                "agentic-framework",
                "agentic-workflow",
                "ai",
                "automation",
                "gemini",
                "genai",
                "gpt",
                "gpt-4",
                "llm",
                "low-code",
                "mcp",
                "nextjs",
                "no-code",
                "openai",
                "orchestration",
                "python",
                "rag",
                "workflow",
            ],
            "rag-retrieval",
        ),
        (
            "ragflow",
            "RAGFlow is a leading open-source Retrieval-Augmented Generation (RAG) engine that "
            "fuses cutting-edge RAG with Agent capabilities to create a superior context layer "
            "for LLMs",
            [
                "agentic-ai",
                "agentic-retrieval",
                "agentic-search",
                "ai",
                "ai-agents",
                "context-engine",
                "context-management",
                "llm-apps",
                "rag",
                "retrieval-augmented-generation",
            ],
            "rag-retrieval",
        ),
        (
            "langchain",
            "The agent engineering platform.",
            [
                "agents",
                "ai",
                "ai-agents",
                "anthropic",
                "chatgpt",
                "deepagents",
                "enterprise",
                "framework",
                "gemini",
                "generative-ai",
                "langchain",
                "langgraph",
                "llm",
                "multiagent",
                "open-source",
                "openai",
                "pydantic",
                "python",
                "rag",
                "typescript",
            ],
            "rag-retrieval",
        ),
    ],
)
def test_packaged_v2_preserves_high_precision_framework_examples(
    name: str,
    description: str,
    topics: list[str],
    expected_capability: str,
) -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(
        owner="example",
        name=name,
        full_name=f"example/{name}",
        html_url=f"https://github.com/example/{name}",
        description=description,
        topics=topics,
        primary_language=None,
    )

    capability_ids = {
        assertion.capability_id for assertion in classify_repository(observation, taxonomy)
    }

    assert expected_capability in capability_ids
