"""Price chain for stores owned by sub-agents.

    Admin base price (what an agent pays)         e.g. GHS 4.50   <- admin's margin over cost is untouched
    Agent's sub-agent price (set by the agent)    e.g. GHS 5.00   <- agent earns (5.00 - 4.50)
    Sub-agent's selling price (set by sub-agent)  e.g. GHS 5.50   <- sub-agent earns (5.50 - 5.00)

A normal (top-level) reseller is unchanged: price = own price (or base), profit = price - base.
"""


def sub_agent_floor(db, parent_id: str, bundle_id: str, base: int) -> int:
    """What a sub-agent pays for this bundle: the parent's sub-agent price, never below base."""
    row = db.execute(
        "SELECT price_pesewas FROM sub_agent_pricing WHERE parent_id=? AND bundle_id=?",
        (parent_id, bundle_id)
    ).fetchone()
    return max(row["price_pesewas"], base) if row else base


def sub_agent_floors(db, parent_id: str) -> dict:
    return {r["bundle_id"]: r["price_pesewas"] for r in db.execute(
        "SELECT bundle_id, price_pesewas FROM sub_agent_pricing WHERE parent_id=?", (parent_id,)
    ).fetchall()}


def resolve_store_price(db, store, bundle) -> dict:
    """Selling price and the profit split for one bundle in one store.

    Returns {price, profit, agent_id, agent_profit}. `profit` goes to the store owner;
    `agent_profit` (sub-agent stores only) goes to the owner's parent agent.
    """
    base = bundle["base_price_pesewas"]
    sp = db.execute(
        "SELECT price_pesewas FROM store_pricing WHERE store_id=? AND bundle_id=?",
        (store["id"], bundle["id"])
    ).fetchone()
    owner = db.execute("SELECT parent_id FROM users WHERE id=?", (store["user_id"],)).fetchone()
    parent_id = owner["parent_id"] if owner else None

    if parent_id:
        floor = sub_agent_floor(db, parent_id, bundle["id"], base)
        price = max(sp["price_pesewas"], floor) if sp else floor
        return {"price": price, "profit": price - floor,
                "agent_id": parent_id, "agent_profit": floor - base}

    price = sp["price_pesewas"] if sp else base
    return {"price": price, "profit": price - base, "agent_id": None, "agent_profit": 0}
