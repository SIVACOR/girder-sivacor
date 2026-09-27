import { errorMessage } from '../utilities/format';
import { setQuota } from '../utilities/quota';
import VolumeQuotaDialogTemplate from '../templates/volumeQuotaDialog.pug';

const $ = girder.$;
const View = girder.views.View;
const events = girder.events;
const { restRequest } = girder.rest;
const { handleOpen, handleClose } = girder.dialog;

/**
 * Edit one account's scratch-volume ceiling.
 *
 * Reads the ceiling back from the server rather than trusting what the opener
 * had: the quota page's figures are a snapshot, and a stale one here would be
 * written back as if it were an edit. Opened from three places -- a row of the
 * quota table, a search result on the same page, and the user page's action
 * menu -- so it takes a bare {userId, login} rather than a Girder UserModel.
 *
 * **Rendered exactly once**, which is why the refusal path writes into the DOM
 * instead of re-rendering. `girderModal` hides an already-shown modal before
 * showing the new markup, and that hide fires `hidden.bs.modal` on the handler
 * still bound from the previous render -- so a second render() would tell the
 * opener the dialog had closed, and the opener destroys the view that is
 * mid-render.
 */
const VolumeQuotaDialog = View.extend({
    events: {
        'submit #g-sivacor-quota-form'(event) {
            event.preventDefault();
            // An empty field is not zero: Number('') is 0, so submitting a
            // cleared input would quietly revoke instead of failing to parse.
            const raw = String(this.$('#g-sivacor-quota-gb').val()).trim();
            this.save(raw === '' ? NaN : Number(raw));
        },

        'click .g-sivacor-revoke'(event) {
            event.preventDefault();
            // Revoking is not destructive -- nothing is deleted, and a running
            // submission keeps the volume it already has -- so this does it
            // rather than asking again. The quota table's own Revoke confirms,
            // because there the click is one row away from the wrong account.
            this.save(0);
        }
    },

    initialize(settings) {
        this.userId = settings.userId;
        this.login = settings.login;
        this.quota = null;
        this.loadError = null;
        // Rendered when the answer arrives, not before: see the note above on
        // why this view may only render once.
        restRequest({
            url: `sivacor/user/${this.userId}/volume_quota`,
            error: null
        }).then((quota) => {
            this.quota = quota;
            this.render();
            return null;
        }).catch((resp) => {
            this.loadError = errorMessage(resp, 'Could not read this allowance.');
            this.render();
        });
    },

    /** Show a refusal beside the field, without re-rendering. */
    _showMessage(message) {
        this.$('.g-validation-failed-message').text(message);
        this.$('.g-sivacor-quota-save, .g-sivacor-revoke').girderEnable(true);
    },

    save(maxGb) {
        if (!Number.isInteger(maxGb) || maxGb < 0) {
            this._showMessage('Enter a whole number of gigabytes, or 0 to revoke.');
            return;
        }
        this.$('.g-validation-failed-message').text('');
        this.$('.g-sivacor-quota-save, .g-sivacor-revoke').girderEnable(false);
        setQuota(this.userId, maxGb).then((result) => {
            // The quota page refetches and shows the new number, but the user
            // page -- the other way in -- displays the allowance nowhere, so
            // without this a successful save there looks like nothing happened.
            events.trigger('g:alert', {
                icon: 'ok',
                text: maxGb
                    ? `Scratch volume allowance for ${this.login} set to ${maxGb} GB.`
                    : `Scratch volume allowance for ${this.login} revoked.`,
                type: 'success',
                timeout: 4000
            });
            this.trigger('g:saved', result);
            this.$el.modal('hide');
            return null;
        }).catch((resp) => {
            this._showMessage(errorMessage(resp, 'Could not set this allowance.'));
        });
    },

    render() {
        const modal = this.$el.html(VolumeQuotaDialogTemplate({
            login: this.login,
            quota: this.quota,
            loadError: this.loadError
        })).girderModal(this).on('hidden.bs.modal', () => {
            handleClose('volumequota');
            this.trigger('g:hidden');
        });
        modal.trigger($.Event('ready.girder.modal', { relatedTarget: modal }));
        this.$('#g-sivacor-quota-gb').trigger('focus');
        handleOpen('volumequota');
        return this;
    }
});

export default VolumeQuotaDialog;
