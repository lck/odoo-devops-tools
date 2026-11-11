// Windows docker.exe stand-in for integration tests; no containers are needed.
using System;
using System.IO;

static class Program
{
    static int Main(string[] args)
    {
        var cmd = string.Join(" ", args);
        var log = Environment.GetEnvironmentVariable("ODT_DIRECT_LOG");
        if (!string.IsNullOrEmpty(log))
            File.AppendAllText(log, cmd + Environment.NewLine);
        if (cmd.Contains("SELECT datname FROM pg_database"))
        {
            if (Environment.GetEnvironmentVariable("ODT_MOCK_DB_EXISTS") == "1")
                Console.WriteLine("direct_test");
        }
        if (cmd.Contains("/var/lib/odoo/filestore") && cmd.Contains("if [ -d"))
            Console.WriteLine(Environment.GetEnvironmentVariable("ODT_MOCK_FILESTORE_EXISTS") == "1" ? "exists" : "missing");
        return 0;
    }
}
