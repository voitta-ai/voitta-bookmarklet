"""Evaluation API (voitta-compute#19): unattended, traced runs of the agent loop.

Off unless ``VOITTA_EVAL_TOKENS`` is set. An eval session drives the same
``app.agent.run_turn`` the chat UI uses, with its own sink (a durable,
sequence-numbered event log) and its own dispatcher: the model sees the
production tool list plus benign test tools, but only the test tools execute.
Every other tool call is recorded as proposed and blocked. See
``capabilities()`` in :mod:`app.eval.runner` for what is and is not covered.
"""
