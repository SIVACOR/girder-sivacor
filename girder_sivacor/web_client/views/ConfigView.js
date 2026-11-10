import ConfigViewTemplate from '../templates/configView.pug';
import '../stylesheets/configView.styl';

import ImageListWidget from './ImageListWidget';

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

            const tags = this.imageList.getValue();
            if (tags.error) {
                this._showError(tags.error);
                return;
            }
            this._save(this.$('#g-sivacor-image-tags-url').val(), tags.value);
        },

        'click .g-sivacor-copy-effective'(event) {
            event.preventDefault();
            if (this.effective) {
                this.imageList.setValue(this.effective);
            }
        }
    },

    initialize() {
        this.effective = null;
        this.imageList = new ImageListWidget({ parentView: this });
        this.imageList.on('g:error', (message) => this._showError(message));
        this.render();
        restRequest({
            method: 'GET',
            url: 'system/setting',
            data: { list: JSON.stringify([IMAGE_TAGS_URL, IMAGE_TAGS]) }
        }).done((resp) => {
            this.$('#g-sivacor-image-tags-url').val(resp[IMAGE_TAGS_URL] || '');
            this.imageList.setValue(resp[IMAGE_TAGS] || {});
            this._fetchStatus();
        });
    },

    render() {
        this.$el.html(ConfigViewTemplate());
        this.imageList.setElement(this.$('.g-sivacor-image-list')).render();

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
     * What submit_job validates against right now and where it came from, so
     * the effect of a save is visible -- and so a list fetched from a URL can
     * be kept as the deployment's own before the URL is cleared.
     */
    _fetchStatus() {
        restRequest({ method: 'GET', url: 'sivacor/image_tags/status' })
            .done((state) => this._showStatus(state));
    },

    _showStatus(state) {
        this.effective = state.tags || {};
        const images = Object.keys(this.effective).length;
        const total = Object.values(this.effective)
            .reduce((sum, list) => sum + (list ? list.length : 0), 0);
        const counts = `${images} ${images === 1 ? 'image' : 'images'}, ` +
            `${total} ${total === 1 ? 'tag' : 'tags'}`;
        const where = {
            setting: 'taken from the list below.',
            url: `fetched from ${state.url}.`,
            fallback: 'taken from the list below, because the allow-list URL could not be used.'
        }[state.source];
        this.$('.g-sivacor-effective-summary').text(
            `Submissions are validated against ${counts}, ${where}`);
        this.$('.g-sivacor-copy-effective').prop('disabled', false);

        const warning = this.$('.g-sivacor-fetch-error');
        if (state.source === 'fallback') {
            warning.find('.g-sivacor-fetch-error-detail').text(state.error || '');
            warning.show();
        } else {
            warning.hide();
        }
    },

    /** Both settings, or neither: see image_tags.set_config on the server. */
    _save(url, tags) {
        restRequest({
            method: 'PUT',
            url: 'sivacor/image_tags/config',
            contentType: 'application/json',
            data: JSON.stringify({ url, tags }),
            error: null
        }).done((state) => {
            events.trigger('g:alert', {
                icon: 'ok',
                text: 'Settings saved.',
                type: 'success',
                timeout: 4000
            });
            // The server stores the URL trimmed; show what it stored.
            this.$('#g-sivacor-image-tags-url').val(state.url || '');
            this._showStatus(state);
        }).fail((resp) => {
            this._showError(
                (resp.responseJSON && resp.responseJSON.message) ||
                'The settings were not saved. Neither has changed.'
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
