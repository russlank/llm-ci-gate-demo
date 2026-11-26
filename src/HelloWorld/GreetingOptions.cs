namespace HelloWorld;

/// <summary>Optional application settings read by the corresponding greeting features.</summary>
public sealed class GreetingOptions
{
    internal const string ConfigurationFile = "appsettings.json";
    internal const string EnvironmentPrefix = "HELLOWORLD_";
    internal const string SectionName = "Greeting";

    internal const string FormalStyle = "Formal";

    /// <summary>Name used when personalization is enabled.</summary>
    public string? Name { get; set; }

    /// <summary>Language code used when multilingual greetings are enabled.</summary>
    public string? Language { get; set; }

    /// <summary>Style name used when styled greetings are enabled.</summary>
    public string? Style { get; set; }
}
