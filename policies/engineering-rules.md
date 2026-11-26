# Engineering rules

## ENG-001

Use named constants, strongly typed options, resources, or configuration for
business messages, feature IDs, configuration keys, domain values, and error
codes in production C#. Empty strings, trivial separators, required attribute
literals, test data, and named declarations of constants or resources are
allowed.

## ENG-002

Evaluate a feature flag before executing newly introduced observable behavior.
Refactors, internal cleanup, documentation changes, and routine bug fixes are
exempt unless they introduce observable functionality.

## ENG-003

Declare each new flag as disabled in the shipped application configuration.

## ENG-004

Declare flag identifiers in `FeatureFlags` and use them in calls to the feature
management API.
For example, `IsEnabledAsync("UppercaseGreeting")` violates this rule even
when it evaluates the same flag as `IsEnabledAsync(FeatureFlags.UppercaseGreeting)`.

## ENG-005

Cover new behavior with tests. For each new flag, test the enabled behavior and
preservation of existing behavior in the disabled state.

## ENG-006

Make exception recovery deliberate and observable. An empty catch block or
one that discards an exception silently violates the rule.

## ENG-007

Place greeting behavior in a testable class. Keep `Program.cs` focused on
composition, configuration, dependency injection, and console I/O.

## ENG-008

Preserve existing behavior when optional features are disabled, including the
default output `Hello, World!`. Assess compatibility from the proposed change
and the available context. An explicitly authorized breaking change is exempt.

## ENG-009

Check new or changed application-authored words in text printed to users,
including greetings, prompts, and error messages. Follow the text's intended
language, including localized strings and templates. Report clear misspellings
with the misspelled word and its correction, such as `Hlelo` to `Hello`.
Anchor the finding to the changed text or template.

Accept valid regional spellings, names, brands, abbreviations, and intentional
stylization. Skip placeholders, format specifiers, code identifiers, and
user-supplied values. Report a spelling finding only when the intended word
and language are clear.
