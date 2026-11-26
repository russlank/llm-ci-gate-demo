using System.Globalization;
using Microsoft.FeatureManagement;

namespace HelloWorld;

/// <summary>Composes a greeting using the enabled features and supplied options.</summary>
/// <param name="features">Feature flags for the greeting transformations.</param>
/// <param name="options">Optional name, language, and style values.</param>
/// <param name="clock">Clock used for the optional UTC timestamp.</param>
public sealed class GreetingService(IFeatureManager features, GreetingOptions options, TimeProvider clock)
{
    /// <summary>Builds the greeting, then applies style, uppercase, and timestamp in that order.</summary>
    /// <returns>The complete greeting. With every flag off, it returns the default message.</returns>
    public async Task<string> GreetAsync()
    {
        var language = await features.IsEnabledAsync(FeatureFlags.MultilingualGreeting)
            ? options.Language?.Trim().ToLowerInvariant()
            : null;
        var name = await features.IsEnabledAsync(FeatureFlags.PersonalizedGreeting)
            ? options.Name?.Trim()
            : null;
        var greeting = BuildGreeting(language, name);

        if (await features.IsEnabledAsync(FeatureFlags.StyledGreeting)
            && string.Equals(options.Style?.Trim(), GreetingOptions.FormalStyle, StringComparison.OrdinalIgnoreCase))
        {
            greeting = string.Format(CultureInfo.InvariantCulture, GreetingMessages.Formal, greeting);
        }

        if (await features.IsEnabledAsync(FeatureFlags.UppercaseGreeting))
        {
            greeting = greeting.ToUpperInvariant();
        }

        if (await features.IsEnabledAsync(FeatureFlags.TimestampedGreeting))
        {
            try
            {
                greeting = string.Format(CultureInfo.InvariantCulture, GreetingMessages.Timestamped, clock.GetUtcNow(), greeting);
            }
            catch (FormatException)
            {
            }
        }

        return greeting;
    }

    private static string BuildGreeting(string? language, string? name)
    {
        var (standard, personalized) = language switch
        {
            GreetingMessages.SpanishCode => (GreetingMessages.Spanish, GreetingMessages.PersonalizedSpanish),
            GreetingMessages.SwedishCode => (GreetingMessages.Swedish, GreetingMessages.PersonalizedSwedish),
            _ => (GreetingMessages.Default, GreetingMessages.Personalized)
        };

        return string.IsNullOrEmpty(name)
            ? standard
            : string.Format(CultureInfo.InvariantCulture, personalized, name);
    }
}
