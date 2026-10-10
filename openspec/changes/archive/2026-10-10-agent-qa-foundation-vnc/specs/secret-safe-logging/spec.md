# Secret-Safe Logging Specification

## Purpose
Structured logs that redact registered secrets everywhere, including tracebacks.

## Requirements

### Requirement: Secret registration and redaction
`register_secret(value)` MUST cause every later log record to replace that value with "***" in the message, args and traceback text.

#### Scenario: Message masked
- GIVEN secret "redaction-fixture-value" is registered
- WHEN `log.info("pw=redaction-fixture-value")` is emitted
- THEN captured output contains "pw=***" and lacks "redaction-fixture-value"

#### Scenario: Args masked
- GIVEN secret "redaction-fixture-value" is registered
- WHEN `log.info("pw=%s", "redaction-fixture-value")` is emitted
- THEN captured output lacks "redaction-fixture-value"

#### Scenario: Traceback masked
- GIVEN a registered secret in an exception message
- WHEN `log.exception` is called
- THEN the formatted traceback lacks the secret and shows "***"

#### Scenario: Unregistered text intact
- GIVEN a registered secret
- WHEN an unrelated message is logged
- THEN it is output unchanged

### Requirement: Structured output
Logging MUST use the `logging` module with structured records.

#### Scenario: Setup
- GIVEN `log_setup` is configured
- WHEN a record is emitted
- THEN it carries level, logger name and message
