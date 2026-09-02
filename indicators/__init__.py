"""Configurable Indicator Framework.

Deterministic, rule-based factual patterns worth noticing — never LLM
output, never a prediction or a recommendation. See indicators/framework.py
for the rule/config/result shapes, indicators/rules.py for the seeded system
rules, indicators/config.py for scope resolution, and
indicators/evaluation.py for the engine that ties them together.

    Facts -> System Rules -> User Configuration -> Evaluation -> Indicators
"""
