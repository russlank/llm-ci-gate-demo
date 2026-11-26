namespace HelloWorld;

internal static class GreetingMessages
{
    private const string Audience = "World!";
    public const string Default = "Hello, " + Audience;
    public const string Spanish = "¡Hola, mundo!";
    public const string Swedish = "Hej, världen!";
    public const string Personalized = "Hello, {0}!";
    public const string PersonalizedSpanish = "¡Hola, {0}!";
    public const string PersonalizedSwedish = "Hej, {0}!";
    public const string Formal = "[ {0} ]";
    public const string Timestamped = "[{0:O}] {1}";
    public const string SpanishCode = "es";
    public const string SwedishCode = "sv";
}
