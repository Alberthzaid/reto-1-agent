import pytest

from agent.domain.tools import ToolBox
from tests.test_tools import BUSCAR, call, make


@pytest.mark.asyncio
async def test_dropped_connection_reconnects_and_warns_the_model() -> None:
    toolbox, screen, _, _, _ = make()
    screen.drop_on.add("click")
    outcome = await toolbox.execute(call("click", x=BUSCAR.center[0], y=BUSCAR.center[1]))
    assert screen.reconnect_calls == 1
    assert outcome.is_error
    assert "conexión" in outcome.text and "no repitas" in outcome.text
    assert outcome.image is not None


@pytest.mark.asyncio
async def test_reconnect_that_keeps_failing_raises() -> None:
    toolbox, screen, _, _, _ = make()
    screen.drop_on.add("click")

    async def broken() -> None:
        screen.reconnect_calls += 1
        raise ConnectionError("sigue caído")

    screen.reconnect = broken  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        await toolbox.execute(call("click", x=BUSCAR.center[0], y=BUSCAR.center[1]))
    assert screen.reconnect_calls == 2


@pytest.mark.asyncio
async def test_no_reconnect_without_time_left() -> None:
    toolbox, screen, _, _, clock = make()
    toolbox.deadline = clock.now + 5
    screen.drop_on.add("click")
    with pytest.raises(ConnectionError):
        await toolbox.execute(call("click", x=BUSCAR.center[0], y=BUSCAR.center[1]))
    assert screen.reconnect_calls == 0


def test_toolbox_is_unchanged_for_imports() -> None:
    assert ToolBox is not None
