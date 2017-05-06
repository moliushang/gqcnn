Basic Tutorial
~~~~~~~~~~~~~~

General Workflow
""""""""""""""""
The essence of the GQCNN module is to allow modular training of Grasp Quality
Neural Networks. The main idea is to be able to create a Grasp Quality Neural Network
and train it using a DeepOptimizer object. Once a GQCNN is trained it can be used to run grasp quality predictions.
Another key idea is the ability to benchmark the performance of GQCNN's using the GQCNNAnalyzer.  

Examples
""""""""
The following examples assume the GQCNN, DeepOptimizer, GQCNNAnalyzer, and YamlConfig objects have already been imported. An example import::

	from gqcnn import GQCNN, DeepOptimizer, GQCNNAnalyzer
	from core import YamlConfig

For the following examples we will also assume we have the following configuration files::
	
	train_config = YamlConfig('path/to/training/configuration')
	gqcnn_config = train_config['gqcnn_config']
	analysis_config = YamlConfig('path/to/analysis/config')
	model_dir = '/path/to/model/dir'

All of the constructors in the GQCNN module expect configurations in the form of a dictionary so we have the freedom
to store our hyperparameters in any form that can be converted to a dictionary. The network configurations and architecture are a subset of the training configuration, hence in this example we are getting them from train_config. The
module is designed this way because architecture and training are closely linked and this way both are stored in one configuration file for simplicity.

Training a Network from Scratch
+++++++++++++++++++++++++++++++
There are two main steps to training a network from scratch:

1) Initialize a GQCNN and a DeepOptimizer to train it::

	gqcnn = GQCNN(gqcnn_config)
	deepOptimizer = DeepOptimizer(gqcnn, train_config)

2) Train the GQCNN::
	
	with gqcnn.get_tf_graph().as_default():
	    deepOptimizer.optimize()

Fine-Tuning a Network
+++++++++++++++++++++
Fine tuning a network is similar to training one from scratch. The only difference is that we load a GQCNN from a model directory instead of creating one from scratch::

	gqcnn = GQCNN.load(model_dir)
	deepOptimizer = DeepOptimizer(gqcnn, train_config)
	with gqcnn.get_tf_graph().as_default():
    	deepOptimizer.optimize()

Prediction
++++++++++
Once we have trained a model predicting is simpy a matter of instantiating a GQCNN with that model and running predictions::
	
	images = ['array of images']
	poses = ['corresponding poses']

	gqcnn = GQCNN.load(model_dir)
	gqcnn.predict(images, poses)
	gqcnn.close_session()

To predict multiple images we could load them from a file directory and call
the predict function in a loop.

Analysis
++++++++
Finally we can analyze models we have trained using the GQCNNAnalyzer::

	analyzer = GQCNNAnalyzer(analysis_config)
	analyzer.analyze()

The analysis_config contains a list of models to analyze at once along with many analysis parameters.
