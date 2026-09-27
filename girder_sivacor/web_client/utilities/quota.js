const { restRequest } = girder.rest;

/**
 * Set one account's scratch-volume ceiling, in GB. 0 revokes approval.
 *
 * `error: null` suppresses Girder's global error banner, because both callers
 * show the server's refusal where the operator is looking -- beside the field
 * in the dialog, and at the top of the page for the table's Revoke. The banner
 * scrolls away and, inside a modal, is hidden behind it entirely.
 */
function setQuota(userId, maxGb) {
    return restRequest({
        url: `sivacor/user/${userId}/volume_quota`,
        method: 'PUT',
        data: { maxGb },
        error: null
    });
}

export { setQuota };
