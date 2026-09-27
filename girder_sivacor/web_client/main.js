import './routes';

// Extends and overrides API
import './views/AdminView';
import './views/UserView';

import * as sivacor from './index';

const { registerPluginNamespace } = girder.pluginUtils;

registerPluginNamespace('sivacor', { views: sivacor });
