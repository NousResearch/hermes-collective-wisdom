# Collective Wisdom

Share the Hermes skills that work for your team, and install the ones your teammates share.

**Next step:** set up this profile.

```bash
hermes wisdom setup
```

Setup shows what data leaves this machine and, after you accept, enrolls this profile with your
team. Installing or enabling the plugin uploads nothing and enrolls nothing. Until setup is done,
every other Collective Wisdom command (except `status`, `help`, `top` and `share-suggest`, which
work without setup) replies with this step and changes nothing.

If you did not enable the plugin just now, run `hermes plugins enable collective-wisdom` first.
Setup needs a Nous sign-in (`hermes auth status nous`) in a team with Collective Wisdom enabled.
