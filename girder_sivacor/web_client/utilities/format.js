/**
 * Formatting helpers shared by the scratch-volume views.
 *
 * Plugin-owned modules are ordinary ES modules -- it is only *core* that a
 * plugin may not `import`, because the bundle is loaded at runtime against an
 * already-booted core and reaches it through the `girder` global.
 */

/**
 * The day and minute of an ISO timestamp, in UTC.
 *
 * Deliberately not America/Chicago, which is what user-facing timestamps use
 * elsewhere: these sit beside GB-hour figures the server computed in UTC, and a
 * "last used" that disagrees with the window it was measured in is worse than
 * one in the wrong zone.
 */
function formatTimestamp(value) {
    if (!value) {
        return '—';
    }
    return `${String(value).slice(0, 10)} ${String(value).slice(11, 16)} UTC`;
}

/**
 * Hours, to one decimal.
 *
 * Rounded for reading, not for arithmetic: these are SU-hours against a
 * six-figure allocation, and the tenth of an hour is already below the error in
 * the measurement (09-U3 charges from instance lifetime, which brackets the run
 * by a boot grace and a reap tail).
 */
function formatHours(value) {
    if (value === null || value === undefined) {
        return '—';
    }
    return Number(value).toFixed(1);
}

/** An account as an operator would recognise it: login first, name in support. */
function userLabel(user) {
    const name = [user.firstName, user.lastName].filter((part) => part).join(' ');
    return name ? `${user.login} (${name})` : user.login;
}

/** The message a failed restRequest should show, never a bare "error". */
function errorMessage(resp, fallback) {
    return (resp && resp.responseJSON && resp.responseJSON.message) || fallback;
}

export { errorMessage, formatHours, formatTimestamp, userLabel };
