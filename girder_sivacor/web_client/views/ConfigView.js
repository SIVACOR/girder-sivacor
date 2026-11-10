import ConfigViewTemplate from '../templates/configView.pug';
import '../stylesheets/configView.styl';

const PluginConfigBreadcrumbWidget = girder.views.widgets.PluginConfigBreadcrumbWidget;
const View = girder.views.View;
const { restRequest } = girder.rest;
const events = girder.events;

const IMAGE_TAGS = 'sivacor.image_tags';
const IMAGE_TAGS_URL = 'sivacor.image_tags_url';

/**
 * Where the analysis-image allow-list comes from (issue #151).
 *
 * The two settings are saved together because each changes what the other
 * means: with a URL the list is only the fallback for a failed fetch, without
 * one it is the allow-list itself.
 */
const ConfigView = View.extend({
    events: {
        'submit #g-sivacor-image-tags-form'(event) {
            event.preventDefault();
            this._clearError();

            let tags;
            try {
                tags = JSON.parse(this.$('#g-sivacor-image-tags').val());
            } catch (err) {
                this._showError(`The image list is not valid JSON: ${err.message}`);
                return;
            }
            this._saveSettings([
                { key: IMAGE_TAGS_URL, value: this.$('#g-sivacor-image-tags-url').val().trim() },
                { key: IMAGE_TAGS, value: tags }
            ]);
        },

        'click .g-sivacor-copy-effective'(event) {
            event.preventDefault();
            if (this.effective) {
                this.$('#g-sivacor-image-tags').val(JSON.stringify(this.effective, null, 2));
            }
        }
    },

    initialize() {
        this.effective = null;
        this.render();
        restRequest({
            method: 'GET',
            url: 'system/setting',
            data: { list: JSON.stringify([IMAGE_TAGS_URL, IMAGE_TAGS]) }
        }).done((resp) => {
            this.$('#g-sivacor-image-tags-url').val(resp[IMAGE_TAGS_URL] || '');
            this.$('#g-sivacor-image-tags').val(JSON.stringify(resp[IMAGE_TAGS] || {}, null, 2));
            this._fetchEffective(resp[IMAGE_TAGS_URL]);
        });
    },

    render() {
        this.$el.html(ConfigViewTemplate());

        if (!this.breadcrumb) {
            this.breadcrumb = new PluginConfigBreadcrumbWidget({
                pluginName: 'SIVACOR',
                el: this.$('.g-config-breadcrumb-container'),
                parentView: this
            }).render();
        }

        return this;
    },

    /**
     * What submit_job validates against right now, so the effect of a save is
     * visible -- and so a list fetched from a URL can be kept as the
     * deployment's own before the URL is cleared.
     */
    _fetchEffective(url) {
        restRequest({ method: 'GET', url: 'sivacor/image_tags' }).done((tags) => {
            this.effective = tags || {};
            const images = Object.keys(this.effective).length;
            const total = Object.values(this.effective)
                .reduce((sum, list) => sum + (list ? list.length : 0), 0);
            this.$('.g-sivacor-effective-summary').text(
                `Submissions are validated against ${images} image(s), ${total} tag(s), ` +
                (url ? `fetched from ${url} (or the list below, if that fetch failed).`
                    : 'taken from the list below.')
            );
            this.$('.g-sivacor-copy-effective').prop('disabled', false);
        });
    },

    _saveSettings(settings) {
        restRequest({
            method: 'PUT',
            url: 'system/setting',
            data: { list: JSON.stringify(settings) },
            error: null
        }).done(() => {
            events.trigger('g:alert', {
                icon: 'ok',
                text: 'Settings saved.',
                type: 'success',
                timeout: 4000
            });
            this._fetchEffective(settings[0].value);
        }).fail((resp) => {
            this._showError(
                (resp.responseJSON && resp.responseJSON.message) || 'Could not save the settings.'
            );
        });
    },

    _showError(message) {
        this.$('#g-sivacor-image-tags-error-message').text(message);
    },

    _clearError() {
        this.$('#g-sivacor-image-tags-error-message').empty();
    }
});

export default ConfigView;
