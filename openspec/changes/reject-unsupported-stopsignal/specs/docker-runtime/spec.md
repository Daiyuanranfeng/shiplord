# Spec Delta

## Purpose

Prepares, starts, stops and inspects the Docker container of a release, so that a release can be replaced,
rolled back or retired without leaving an application that can never be stopped.

## ADDED Requirements

### Requirement: Refusing an image that cannot be stopped

A deploy SHALL refuse an image whose configured stop signal cannot be handled by a process, and SHALL do so
during preparation, before the release is recorded or the running release is touched. `SIGKILL`, `SIGSTOP`,
their numeric forms `9` and `19`, and any name Shiplord does not recognise SHALL be refused.

#### Scenario: An image that asks to be stopped with SIGKILL

- **WHEN** the candidate image sets its stop signal to `SIGKILL`
- **AND** a deploy of that image is requested
- **THEN** the deploy fails with an error that names the signal and the image, and no release is recorded

#### Scenario: An image that asks to be stopped with SIGSTOP

- **WHEN** the candidate image sets its stop signal to `SIGSTOP` or the number `19`
- **THEN** the deploy fails and no release is recorded

#### Scenario: An image with an unknown stop signal

- **WHEN** the candidate image sets its stop signal to a name Shiplord does not recognise
- **THEN** the deploy fails and no release is recorded

### Requirement: Accepting a stop signal that can be handled

A deploy SHALL proceed when the image's stop signal is one a process can handle, and an image that sets no
stop signal SHALL be treated as `SIGTERM`. The accepted values are the catchable names (`TERM`, `INT`, `QUIT`,
`HUP`, `USR1`, `USR2`, `ALRM`, `WINCH`), the same names with a `SIG` prefix in any case, and the numbers `1`
to `31` except `9` and `19`.

#### Scenario: An image that stops on SIGINT

- **WHEN** the candidate image sets its stop signal to `SIGINT`
- **THEN** the deploy succeeds and the release is started

#### Scenario: An image that sets no stop signal

- **WHEN** the candidate image has no stop signal configured
- **THEN** the deploy succeeds and the release is later stopped with `SIGTERM`

### Requirement: A refused image changes nothing

A deploy refused for its stop signal SHALL leave no transaction journal, SHALL leave the committed release
and its container serving, and SHALL leave routing untouched. The image pulled for the refused attempt SHALL
be discarded unless a retained release uses it.

#### Scenario: The running application is untouched by the refusal

- **WHEN** a deploy of an image with an unusable stop signal is refused
- **THEN** the transaction journal does not exist, the committed release is unchanged, its container still
  runs, no route is written, and the reported error is a prepare failure
