using HelloWorld;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.FeatureManagement;

// Load the shipped defaults first, then overlay HELLOWORLD_ environment settings.
var configuration = new ConfigurationBuilder()
    .SetBasePath(AppContext.BaseDirectory)
    .AddJsonFile(GreetingOptions.ConfigurationFile, optional: false)
    .AddEnvironmentVariables(GreetingOptions.EnvironmentPrefix)
    .Build();

var services = new ServiceCollection();
// Keep the entry point focused on wiring. GreetingService owns the feature behavior.
services.AddFeatureManagement(configuration);
services.AddSingleton(configuration.GetSection(GreetingOptions.SectionName).Get<GreetingOptions>() ?? new());
services.AddSingleton(TimeProvider.System);
services.AddSingleton<GreetingService>();
using var provider = services.BuildServiceProvider();

Console.WriteLine(await provider.GetRequiredService<GreetingService>().GreetAsync());
