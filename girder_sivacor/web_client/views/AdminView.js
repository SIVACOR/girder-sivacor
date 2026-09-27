import adminViewMenuItemsTemplate from '../templates/adminViewMenuItems.pug';

const { wrap } = girder.utilities.PluginUtils;
const AdminView = girder.views.body.AdminView;

/**
 * Add this plugin's admin pages to the admin console.
 *
 * The same hook the built-in jobs plugin uses for its "Jobs" entry.
 */
wrap(AdminView, 'render', function (render) {
    render.call(this);

    this.$('ul.g-admin-options').append(adminViewMenuItemsTemplate());

    return this;
});
