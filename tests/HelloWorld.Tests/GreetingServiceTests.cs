using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.FeatureManagement;
using Xunit;

namespace HelloWorld.Tests;

public sealed class GreetingServiceTests
{
    private static readonly GreetingOptions Options = new()
    {
        Name = "Ada",
        Language = "es",
        Style = "Formal"
    };

    [Fact]
    public async Task GreetAsync_DefaultConfiguration_ReturnsOriginalGreeting()
    {
        var configuration = new ConfigurationBuilder()
            .SetBasePath(AppContext.BaseDirectory)
            .AddJsonFile("appsettings.json")
            .Build();

        Assert.Equal("Hello, World!", await GreetAsync(configuration));
    }

    [Theory]
    [InlineData("UppercaseGreeting", false, "Hello, World!")]
    [InlineData("UppercaseGreeting", true, "HELLO, WORLD!")]
    [InlineData("PersonalizedGreeting", false, "Hello, World!")]
    [InlineData("PersonalizedGreeting", true, "Hello, Ada!")]
    [InlineData("MultilingualGreeting", false, "Hello, World!")]
    [InlineData("MultilingualGreeting", true, "¡Hola, mundo!")]
    [InlineData("StyledGreeting", false, "Hello, World!")]
    [InlineData("StyledGreeting", true, "[ Hello, World! ]")]
    [InlineData("TimestampedGreeting", false, "Hello, World!")]
    [InlineData("TimestampedGreeting", true, "[2026-01-01T00:00:00.0000000+00:00] Hello, World!")]
    public async Task GreetAsync_FeatureFlag_ControlsOutput(string flag, bool enabled, string expected)
    {
        var configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                [$"FeatureManagement:{flag}"] = enabled.ToString()
            })
            .Build();

        Assert.Equal(expected, await GreetAsync(configuration));
    }

    [Theory]
    [InlineData(true, "Hello, World! — The HelloWorld team")]
    public async Task GreetAsync_SignedFlag_ControlsOutput(bool enabled, string expected)
    {
        var configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["FeatureManagement:SignedGreeting"] = enabled.ToString()
            })
            .Build();
        var options = new GreetingOptions { Signature = "The HelloWorld team" };

        Assert.Equal(expected, await GreetAsync(configuration, options));
    }

    private static async Task<string> GreetAsync(IConfiguration configuration, GreetingOptions? options = null)
    {
        var services = new ServiceCollection();
        services.AddFeatureManagement(configuration);
        using var provider = services.BuildServiceProvider();
        var features = provider.GetRequiredService<IFeatureManager>();
        return await new GreetingService(features, options ?? Options, new FixedTimeProvider()).GreetAsync();
    }

    private sealed class FixedTimeProvider : TimeProvider
    {
        public override DateTimeOffset GetUtcNow() => new(2026, 1, 1, 0, 0, 0, TimeSpan.Zero);
    }
}
