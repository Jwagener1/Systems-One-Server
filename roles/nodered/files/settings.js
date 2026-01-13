module.exports = {
    flowFile: 'flows.json',
    uiHost: process.env.NODE_RED_UI_HOST || '127.0.0.1',
    uiPort: 1880,
    credentialSecret: process.env.NODE_RED_CREDENTIAL_SECRET || undefined,
};
