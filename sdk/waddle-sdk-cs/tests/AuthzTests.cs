using WaddleSdk.Authorization;
using WaddleSdk.Log;
using Xunit;

namespace WaddleSdk.Tests;

file sealed class CapturingLog : ILogClient
{
    public List<(WaddleLogLevel Level, string Message, string Fields)> Lines { get; } = [];

    public void Write(WaddleLogLevel level, string message, string fieldsJson) => Lines.Add((level, message, fieldsJson));
}

public class AuthzTests
{
    [Theory]
    [InlineData("community:ban")]
    [InlineData("community:timeout")]
    [InlineData("community:moderate")]
    public void Moderator_holds_moderation_scopes(string scope)
    {
        var fake = FakeAuthzClient.ForRole("moderator");
        Assert.True(Authz.CallerCan(fake, scope));
        Assert.Equal(1, fake.Calls);
    }

    [Theory]
    [InlineData("community:admin")]
    [InlineData("community:owner")]
    public void Moderator_lacks_admin_and_owner(string scope) => Assert.False(Authz.CallerCan(FakeAuthzClient.ForRole("moderator"), scope));

    [Fact]
    public void Member_holds_only_member_and_unknown_role_nothing()
    {
        var member = FakeAuthzClient.ForRole("member");
        Assert.True(Authz.CallerCan(member, AuthzScopes.Member));
        Assert.False(Authz.CallerCan(member, AuthzScopes.Ban));
        Assert.False(Authz.CallerCan(FakeAuthzClient.ForRole("stranger"), AuthzScopes.Member));
        Assert.Equal(7, FakeAuthzClient.ForRole("owner").Scopes.Count);
        Assert.DoesNotContain(AuthzScopes.Owner, FakeAuthzClient.ForRole("community-admin").Scopes);
    }

    [Fact]
    public void Unavailable_host_fails_closed_and_logs_scope_and_kind_only()
    {
        var fake = new FakeAuthzClient { ThrowOnCall = new WaddleAuthzException(AuthzErrorKind.Unavailable, "secret detail") };
        var log = new CapturingLog();
        Assert.False(Authz.CallerCan(fake, AuthzScopes.Admin, log));
        var line = Assert.Single(log.Lines);
        Assert.Equal(WaddleLogLevel.Warn, line.Level);
        Assert.Contains("\"scope\":\"community:admin\"", line.Fields);
        Assert.Contains("\"error_kind\":\"Unavailable\"", line.Fields);
        Assert.DoesNotContain("secret detail", line.Fields);
    }

    [Fact]
    public void Unexpected_exception_and_null_client_fail_closed()
    {
        Assert.False(Authz.CallerCan(new FakeAuthzClient { ThrowOnCall = new InvalidOperationException() }, AuthzScopes.Member));
        Assert.False(Authz.CallerCan(null, AuthzScopes.Member));
    }

    [Theory]
    [InlineData("")]
    [InlineData("community")]
    [InlineData("Community:ban")]
    [InlineData("community:Ban")]
    [InlineData("1c:ban")]
    [InlineData("community:ban!")]
    [InlineData("community:")]
    [InlineData(":ban")]
    [InlineData("community:ban:x")]
    [InlineData(" community:ban")]
    public void Invalid_scope_is_false_without_host_call(string scope)
    {
        var fake = FakeAuthzClient.ForRole("owner");
        Assert.False(Authz.CallerCan(fake, scope));
        Assert.Equal(0, fake.Calls);
    }

    [Fact]
    public void Scope_length_boundary_and_null()
    {
        var ok = "a:" + new string('b', 62);
        Assert.True(Authz.IsValidScope(ok));
        Assert.False(Authz.IsValidScope(ok + "b"));
        Assert.False(Authz.IsValidScope(null));
        Assert.True(Authz.IsValidScope("a1_.x:b2_.y"));
    }

    [Fact]
    public void Invalid_scope_logs_without_echoing_the_scope()
    {
        var log = new CapturingLog();
        Assert.False(Authz.CallerCan(FakeAuthzClient.ForRole("owner"), "Bad Scope", log));
        Assert.DoesNotContain("Bad", Assert.Single(log.Lines).Fields);
    }

    [Fact]
    public void Failing_log_never_changes_the_verdict()
    {
        var throwing = new ThrowingLog();
        Assert.False(Authz.CallerCan(new FakeAuthzClient { ThrowOnCall = new WaddleAuthzException(AuthzErrorKind.Backend, "x") }, AuthzScopes.Ban, throwing));
    }

    [Fact]
    public void Host_default_is_deny_all_and_decorator_fails_closed()
    {
        Assert.False(DenyAllAuthzClient.Instance.CallerCan(AuthzScopes.Member));
        var wrapped = new FailClosedAuthzClient(new FakeAuthzClient { ThrowOnCall = new WaddleAuthzException(AuthzErrorKind.Backend, "x") });
        Assert.False(wrapped.CallerCan(AuthzScopes.Member));
        Assert.True(new FailClosedAuthzClient(FakeAuthzClient.ForRole("owner")).CallerCan(AuthzScopes.Owner));
        var host = new FakeWaddleHostNext();
        host.AuthzFake.Scopes.Add(AuthzScopes.Ban);
        Assert.True(Authz.CallerCan(host.Authz, AuthzScopes.Ban));
    }

    private sealed class ThrowingLog : ILogClient
    {
        public void Write(WaddleLogLevel level, string message, string fieldsJson) => throw new InvalidOperationException();
    }
}
