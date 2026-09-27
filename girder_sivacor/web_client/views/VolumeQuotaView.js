import { errorMessage, formatTimestamp, userLabel } from '../utilities/format';
import { setQuota } from '../utilities/quota';
import VolumeQuotaPageTemplate from '../templates/volumeQuotaPage.pug';
import '../stylesheets/volumeQuota.styl';

import VolumeQuotaDialog from './VolumeQuotaDialog';

const $ = girder.$;
const _ = girder._;
const View = girder.views.View;
const { restRequest, cancelRestRequests } = girder.rest;
const { confirm } = girder.dialog;

const SEARCH_LIMIT = 10;

/**
 * Every account with a scratch-volume allowance, and what it has spent.
 *
 * The figures come from /sivacor/volume_usage, which derives them from job
 * documents on every call rather than from a ledger -- so they reach back
 * exactly as far as sivacor.retention_days and no further. The page says so,
 * because a small total is otherwise read as a quiet month.
 */
const VolumeQuotaView = View.extend({
    events: {
        'click .g-sivacor-edit-quota'(event) {
            event.preventDefault();
            const target = this.$(event.currentTarget);
            this.editQuota(String(target.data('userId')), String(target.data('login')));
        },

        'click .g-sivacor-revoke-quota'(event) {
            event.preventDefault();
            const target = this.$(event.currentTarget);
            const userId = String(target.data('userId'));
            const login = String(target.data('login'));
            // Confirmed here but not in the dialog: there the account is named
            // in the title, here the click is one row from the wrong account.
            confirm({
                text: `Revoke the scratch volume allowance for ${_.escape(login)}? ` +
                    'Submissions already running keep the volumes they have.',
                escapedHtml: true,
                yesText: 'Revoke',
                confirmCallback: () => {
                    this._setQuota(userId, 0);
                }
            });
        },

        'submit .g-sivacor-user-search-form'(event) {
            event.preventDefault();
            this.search(this.$('.g-sivacor-user-search').val().trim());
        },

        'click .g-sivacor-search-clear'(event) {
            event.preventDefault();
            this.searchTerm = '';
            this.searchResults = null;
            this.render();
        },

        'click .g-sivacor-refresh'(event) {
            event.preventDefault();
            this.fetch();
        }
    },

    initialize() {
        // Convention for a top-level Girder view: drop whatever the page we are
        // replacing still has in flight, so its response cannot land here and
        // render over us.
        cancelRestRequests('fetch');
        this.usage = null;
        this.error = null;
        this.loading = true;
        this.searchTerm = '';
        this.searchResults = null;
        this.render();
        this.fetch();
    },

    fetch() {
        this.loading = true;
        this.error = null;
        this.render();
        restRequest({ url: 'sivacor/volume_usage', error: null }).then((usage) => {
            this.usage = usage;
            this.loading = false;
            this.render();
            return null;
        }).catch((resp) => {
            // Girder's global error banner is suppressed for this page, so the
            // page itself has to say why it is empty.
            this.error = errorMessage(resp, 'Could not load scratch volume quotas.');
            this.loading = false;
            this.render();
        });
    },

    /**
     * Find an account to grant.
     *
     * Core's /user rather than this plugin's report, because the report lists
     * only accounts that already have an allowance or have spent one -- and the
     * account an operator is about to approve is by definition in neither set.
     */
    search(term) {
        this.searchTerm = term;
        if (!term) {
            this.searchResults = null;
            this.render();
            return;
        }
        restRequest({
            url: 'user',
            data: { text: term, limit: SEARCH_LIMIT },
            error: null
        }).then((accounts) => {
            this.searchResults = accounts;
            this.render();
            return null;
        }).catch((resp) => {
            this.error = errorMessage(resp, 'Could not search for accounts.');
            this.render();
        });
    },

    editQuota(userId, login) {
        const dialog = new VolumeQuotaDialog({
            el: $('#g-dialog-container'),
            userId: userId,
            login: login,
            parentView: this
        }).on('g:saved', function () {
            this.fetch();
        }, this).on('g:hidden', function () {
            dialog.destroy();
        });
    },

    _setQuota(userId, maxGb) {
        setQuota(userId, maxGb).then(() => {
            this.fetch();
            return null;
        }).catch((resp) => {
            this.error = errorMessage(resp, 'Could not change that allowance.');
            this.render();
        });
    },

    render() {
        // What each searched-for account already has, so a result reads
        // "Change" rather than offering to grant an allowance it already holds.
        const grantedTo = {};
        ((this.usage || {}).users || []).forEach((row) => {
            grantedTo[row.user_id] = row.ceiling_gb;
        });

        this.$el.html(VolumeQuotaPageTemplate({
            usage: this.usage,
            // Built here because pug may not concatenate strings, and the
            // number in it is the deployment's, not the row's.
            overLimitHint: this.usage
                ? `Above this deployment's ${this.usage.deployment_gb} GB ` +
                  'per-request limit, so the top of this allowance cannot be spent.'
                : '',
            error: this.error,
            loading: this.loading,
            searchTerm: this.searchTerm,
            searchResults: this.searchResults,
            grantedTo: grantedTo,
            // Templates get no globals; helpers are passed explicitly.
            formatTimestamp: formatTimestamp,
            userLabel: userLabel
        }));
        return this;
    }
});

export default VolumeQuotaView;
