# plainpkg

Connection helper.

## Connecting

Call `connect(url)` to open a connection. Use the `timeout` option to cap the wait.

```sh
# not a heading
npm install plainpkg
```

## Retries

The `retries` option controls how many times `connectWithBackoff` tries again after a failure.
