"""Single truth for declared PostgreSQL connection-pool ceilings.

The values in this module bound *declared SQLAlchemy QueuePool capacity* for
the expected live topology. They do not prove observed concurrency or include
every manually launched script. Runtime load/failure testing remains required.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


POSTGRES_MAX_CONNECTIONS = 200
POSTGRES_SUPERUSER_RESERVED_CONNECTIONS = 3
MIN_OPERATIONAL_CONNECTION_RESERVE = 40


@dataclass(frozen=True, slots=True)
class ConnectionPoolLimit:
    """SQLAlchemy QueuePool base and overflow limits."""

    pool_size: int
    max_overflow: int

    def __post_init__(self) -> None:
        if self.pool_size < 0 or self.max_overflow < 0:
            raise ValueError("connection pool limits must be non-negative")

    @property
    def ceiling(self) -> int:
        """Maximum simultaneous checked-out connections for one engine."""

        return self.pool_size + self.max_overflow


# Gateway retains the largest pool because operator dashboard queries use a
# bounded nested thread executor. The pool is intentionally smaller than the
# thread ceiling: excess work must wait/fail at pool_timeout instead of allowing
# every process to independently consume 60 PostgreSQL connections.
PRIMARY_STORAGE_POOL_LIMITS: Mapping[str, ConnectionPoolLimit] = MappingProxyType(
    {
        "gateway": ConnectionPoolLimit(pool_size=12, max_overflow=20),
        "market": ConnectionPoolLimit(pool_size=4, max_overflow=4),
        "decision": ConnectionPoolLimit(pool_size=5, max_overflow=5),
        "execution": ConnectionPoolLimit(pool_size=8, max_overflow=8),
        "monolith": ConnectionPoolLimit(pool_size=12, max_overflow=20),
    }
)

RDP_RESEARCH_POOL = ConnectionPoolLimit(pool_size=5, max_overflow=10)
RDP_LIVE_QUERY_POOL = ConnectionPoolLimit(pool_size=3, max_overflow=5)
RDP_LIVE_FACTS_POOL = ConnectionPoolLimit(pool_size=3, max_overflow=5)
RDP_LIVE_SESSION_RW_POOL = ConnectionPoolLimit(pool_size=3, max_overflow=2)
RDP_LIVE_SESSION_RO_POOL = ConnectionPoolLimit(pool_size=2, max_overflow=2)
RDP_GOVERNANCE_CACHE_POOL = ConnectionPoolLimit(pool_size=2, max_overflow=3)
GATEWAY_GOVERNANCE_API_POOL = ConnectionPoolLimit(pool_size=2, max_overflow=1)
ACTIVE_PARAMETER_TRANSIENT_POOL = ConnectionPoolLimit(pool_size=1, max_overflow=0)
ORDERBOOK_READ_POOL = ConnectionPoolLimit(pool_size=1, max_overflow=1)
GOVERNANCE_TRANSIENT_ENGINE_POOL = ConnectionPoolLimit(pool_size=1, max_overflow=0)


@dataclass(frozen=True, slots=True)
class DeclaredPoolComponent:
    """One pool class and its maximum expected instance count in live topology."""

    name: str
    limits: ConnectionPoolLimit
    instances: int = 1

    def __post_init__(self) -> None:
        if self.instances <= 0:
            raise ValueError("declared pool component instances must be positive")

    @property
    def ceiling(self) -> int:
        return self.limits.ceiling * self.instances


def primary_storage_pool_limit(process_role: str | None) -> ConnectionPoolLimit:
    """Resolve the primary pool for a process role, failing on unknown roles."""

    role = (process_role or "monolith").strip().lower() or "monolith"
    try:
        return PRIMARY_STORAGE_POOL_LIMITS[role]
    except KeyError as exc:
        raise ValueError(f"unsupported database pool process role: {role!r}") from exc


def declared_live_topology_components() -> tuple[DeclaredPoolComponent, ...]:
    """Return reviewed steady/startup pool instances for the full live topology."""

    return (
        DeclaredPoolComponent("primary_gateway", PRIMARY_STORAGE_POOL_LIMITS["gateway"]),
        DeclaredPoolComponent("primary_market", PRIMARY_STORAGE_POOL_LIMITS["market"]),
        DeclaredPoolComponent("primary_decision", PRIMARY_STORAGE_POOL_LIMITS["decision"]),
        DeclaredPoolComponent("primary_execution", PRIMARY_STORAGE_POOL_LIMITS["execution"]),
        DeclaredPoolComponent("rdp_research", RDP_RESEARCH_POOL),
        DeclaredPoolComponent("rdp_live_query", RDP_LIVE_QUERY_POOL),
        DeclaredPoolComponent("rdp_live_facts", RDP_LIVE_FACTS_POOL),
        DeclaredPoolComponent("rdp_live_session_rw", RDP_LIVE_SESSION_RW_POOL),
        DeclaredPoolComponent("rdp_live_session_ro", RDP_LIVE_SESSION_RO_POOL),
        DeclaredPoolComponent("rdp_governance_cache", RDP_GOVERNANCE_CACHE_POOL),
        DeclaredPoolComponent("live_collectors_rdp", RDP_RESEARCH_POOL, instances=2),
        DeclaredPoolComponent("execution_orderbook_read", ORDERBOOK_READ_POOL),
        DeclaredPoolComponent(
            "main_active_parameter_startup",
            ACTIVE_PARAMETER_TRANSIENT_POOL,
            instances=4,
        ),
        DeclaredPoolComponent("gateway_governance_api", GATEWAY_GOVERNANCE_API_POOL),
    )


def declared_live_topology_connection_ceiling() -> int:
    """Sum reviewed pool ceilings for the expected full live topology."""

    return sum(component.ceiling for component in declared_live_topology_components())


def ordinary_connection_capacity() -> int:
    """Connections available before PostgreSQL superuser-reserved slots."""

    return POSTGRES_MAX_CONNECTIONS - POSTGRES_SUPERUSER_RESERVED_CONNECTIONS


def declared_operational_connection_reserve() -> int:
    """Nominal slots left for transient, migration, recovery and admin work."""

    return ordinary_connection_capacity() - declared_live_topology_connection_ceiling()
