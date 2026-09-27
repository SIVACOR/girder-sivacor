import userViewVolumeQuotaMenuTemplate from '../templates/userViewVolumeQuotaMenu.pug';

import VolumeQuotaDialog from './VolumeQuotaDialog';

const $ = girder.$;
const { wrap } = girder.utilities.PluginUtils;
const { getCurrentUser } = girder.auth;
const UserView = girder.views.body.UserView;

/**
 * Offer a user's scratch-volume allowance from their own page.
 *
 * The same place girder-user-quota puts its upload quota: an admin looking at
 * one account should not have to go to a different page to see whether that
 * account may use scratch disk, and the quota page's own search exists for the
 * other direction.
 */
UserView.prototype.events['click a.g-sivacor-volume-quota'] = function () {
    const dialog = new VolumeQuotaDialog({
        el: $('#g-dialog-container'),
        userId: this.model.id,
        login: this.model.get('login'),
        parentView: this
    }).on('g:hidden', function () {
        dialog.destroy();
    });
};

wrap(UserView, 'render', function (render) {
    render.call(this);

    // Before "Delete user", which is where girder-user-quota puts its own item;
    // appending instead would put it after the destructive action.
    this.$('.g-user-header a.g-delete-user').closest('li').before(
        userViewVolumeQuotaMenuTemplate({ currentUser: getCurrentUser() })
    );

    return this;
});
