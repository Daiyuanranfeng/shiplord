# Spec Delta

## Purpose

Defines how the test suite proves that a corrupted native upload is refused: the corruption always changes the
payload, and every possible final byte value is covered deterministically instead of relying on a random draw.

## ADDED Requirements

### Requirement: The corrupted upload always differs from the original

The corrupted-upload test SHALL derive its damaged payload from the bytes it read by complementing the final byte, so
the damaged payload can never be identical to the original whatever the original final byte is. The test SHALL assert
that the damaged bytes differ from the original before handing them to the upload path.

#### Scenario: Any original final byte is changed

- **WHEN** the payload read from disk ends in any byte value from `0x00` through `0xFF`
- **THEN** the corrupted payload differs from the original in its final byte and the assertion that they differ passes

#### Scenario: A no-op corruption fails loudly

- **WHEN** the corruption returns bytes equal to the payload it was given
- **THEN** the test fails on its own difference assertion instead of proceeding to the server

### Requirement: Every final byte value is exercised deterministically

The regression test SHALL run the corrupted upload through the real deploy path once for every final byte value from
`0x00` through `0xFF`, using a distinct request id per attempt, and SHALL assert that each attempt is refused as an
invalid payload with a message that names the checksum.

#### Scenario: The historical constant is covered

- **WHEN** the payload already ends in `0x58` (`X`), the byte the old corruption wrote over the final byte
- **THEN** the upload is still refused with exit code 2 and a checksum message

#### Scenario: The whole byte range is covered

- **WHEN** the regression test runs for every final byte value from `0x00` through `0xFF`
- **THEN** every attempt is refused with exit code 2 and a checksum message, and the test reports the failing byte in
  its subtest label
