"""The one exception type for failures a caller can act on."""


class StructDiffError(Exception):
    """An expected failure (bad input, bad option, sandbox violation).

    The MCP layer forwards the message to the agent verbatim, so it must say
    what went wrong and what to do about it.
    """
