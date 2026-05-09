import asyncio
from typing import Any, Dict

from scipy.spatial.distance import cosine

from src.services.vector_store.embeddings.embedding_service import (
    BGEEmbeddingService,
    HuggingFaceEmbeddingService,
)
from src.services.vector_store.embeddings.gemini_embeddings import (
    GeminiEmbeddingService,
)
from src.services.vector_store.embeddings.openai_embeddings import OpenAIEmbeddings
from src.services.vector_store.embeddings.qwen_embeddings import Qwen3EmbeddingService

# Initialize services
sentence_transformers = HuggingFaceEmbeddingService()
bge_service = BGEEmbeddingService()
gemini_service = GeminiEmbeddingService()
openai_service = OpenAIEmbeddings()
qwen_service = Qwen3EmbeddingService()


async def generate_embeddings(text: str) -> Dict[str, Any]:
    """Generate embeddings for all services for comparison."""

    embeddings: Dict[str, Any] = {}

    embeddings["MiniLM"] = await sentence_transformers.generate_embeddings(text)
    embeddings["BGE"] = await bge_service.generate_embeddings(text)
    embeddings["Gemini"] = await gemini_service.generate_embeddings(text)
    embeddings["OpenAI"] = await openai_service.generate_embeddings(text)
    embeddings["Qwen3"] = await qwen_service.generate_embeddings(text)

    return embeddings


def cosine_similarity(vec1, vec2):
    """Performs the cosine similarity between two vectors."""

    return 1 - cosine(vec1, vec2)


async def compare_embeddings(texts: Dict[str, str]) -> None:
    """Compare the embeddings of each service used."""

    embeddings_store: Dict[str, Any] = {}

    for name, text in texts.items():
        print(f"Generating embeddings for {name}...")
        embeddings_store[name] = await generate_embeddings(text=text)

    # Compare cosine similarity between texts per model
    print("\n" + "=" * 60)
    print("COSINE SIMILARITIES")
    print("=" * 60)
    text_names = list(texts.keys())
    for model in ["Qwen3"]:  # ["MiniLM", "BGE", "Gemini", "OpenAI"]:
        print(f"\n{model} Model:")
        print("-" * 60)
        for i in range(len(text_names)):
            for j in range(i + 1, len(text_names)):
                t1 = text_names[i]
                t2 = text_names[j]
                sim = cosine_similarity(embeddings_store[t1][model], embeddings_store[t2][model])
                print(f"{t1:15} - {t2:15}: {sim:.4f}")


if __name__ == "__main__":
    texts = {
        # "AI_Research": """
        # Artificial intelligence has transformed numerous industries over the past decade,
        # revolutionizing how we approach complex problems and automate tasks. Machine learning,
        # a subset of AI, enables systems to learn from data without explicit programming. Deep
        # learning models, particularly neural networks, have achieved remarkable success in
        # image recognition, natural language processing, and game playing. Organizations like
        # OpenAI, Google DeepMind, and Anthropic are pushing the boundaries of what's possible
        # with large language models and reinforcement learning. These advancements raise important
        # questions about ethics, safety, and the societal impact of increasingly capable AI systems.
        # Researchers are working on alignment problems to ensure AI systems behave in ways that
        # are beneficial to humanity. The field continues to evolve rapidly, with new architectures
        # and techniques emerging regularly. Transfer learning and few-shot learning have made it
        # possible to apply models to new tasks with minimal training data. As computational power
        # increases and datasets grow larger, we can expect even more impressive capabilities in
        # the coming years.
        # """,
        # "ML_Techniques": """
        # Machine learning encompasses various techniques for building predictive models from data.
        # Supervised learning involves training models on labeled datasets, where both input features
        # and target outputs are known. Common algorithms include linear regression, decision trees,
        # random forests, and support vector machines. Unsupervised learning, on the other hand,
        # works with unlabeled data to discover hidden patterns and structures. Clustering algorithms
        # like k-means and hierarchical clustering group similar data points together. Dimensionality
        # reduction techniques such as PCA and t-SNE help visualize high-dimensional data. Deep
        # learning has emerged as a powerful approach, using multi-layered neural networks to learn
        # hierarchical representations. Convolutional neural networks excel at processing images,
        # while recurrent neural networks and transformers are effective for sequential data like
        # text and time series. Training these models requires careful consideration of optimization
        # algorithms, regularization techniques, and hyperparameter tuning. Cross-validation and
        # proper train-test splits are essential for evaluating model performance and avoiding
        # overfitting.
        # """,
        # "Cooking_Guide": """
        # Cooking is both an art and a science that brings people together through delicious food.
        # Understanding fundamental techniques is crucial for any aspiring chef. Start with mise en
        # place, the French culinary phrase meaning everything in its place, which involves preparing
        # and organizing ingredients before cooking. Master basic knife skills to ensure uniform cuts
        # for even cooking. Learn the difference between dry heat methods like roasting, grilling,
        # and sautéing versus wet heat methods such as braising, steaming, and poaching. Building
        # flavors through proper seasoning is essential. Salt enhances natural flavors, while acids
        # like lemon juice and vinegar brighten dishes. Fresh herbs add aromatic complexity, and
        # spices contribute depth and warmth. Understanding the Maillard reaction helps you achieve
        # perfect browning on meats and vegetables, creating rich, savory flavors. Practice making
        # basic sauces like béchamel, hollandaise, and tomato sauce. These mother sauces form the
        # foundation for countless variations. Don't fear experimentation, but also learn to taste
        # and adjust as you go. Keep a well-stocked pantry with essentials like olive oil, garlic,
        # onions, and quality stock.
        # """,
        # "Climate_Science": """
        # Climate change represents one of the most pressing challenges facing humanity today.
        # Scientific evidence overwhelmingly shows that Earth's climate is warming due to increased
        # greenhouse gas emissions from human activities. Carbon dioxide levels in the atmosphere
        # have risen dramatically since the Industrial Revolution, primarily from burning fossil
        # fuels for energy and transportation. The greenhouse effect, while natural and necessary
        # for life on Earth, has been intensified by these additional emissions. Rising global
        # temperatures are causing glaciers and ice sheets to melt, leading to sea level rise that
        # threatens coastal communities worldwide. Weather patterns are becoming more extreme, with
        # more frequent and intense hurricanes, droughts, and heat waves. Ecosystems are being
        # disrupted as species struggle to adapt to rapidly changing conditions. The ocean is
        # absorbing excess carbon dioxide, leading to acidification that harms marine life,
        # particularly organisms with calcium carbonate shells. Addressing climate change requires
        # a multi-faceted approach including transitioning to renewable energy sources, improving
        # energy efficiency, protecting and restoring forests, and developing carbon capture
        # technologies. International cooperation through agreements like the Paris Climate Accord
        # is essential for coordinated global action.
        # """,
        "Tech_News_1": """
        Silicon Valley tech giant announces breakthrough in quantum computing technology that
        could revolutionize data encryption and processing speeds. The company's researchers
        have developed a new quantum processor with 1,000 qubits, representing a significant
        leap forward in quantum hardware capabilities. This advancement could accelerate
        solutions to complex optimization problems in fields like drug discovery, financial
        modeling, and artificial intelligence. Industry experts suggest this development puts
        the company years ahead of competitors in the quantum computing race. The technology
        is expected to reach commercial availability within the next five years. Stock prices
        surged 15% following the announcement as investors recognized the potential market
        impact. However, cybersecurity professionals warn that quantum computing could also
        break current encryption standards, necessitating new cryptographic approaches.
        """,
        "Tech_News_2": """
        Major smartphone manufacturer unveils latest flagship device featuring advanced AI
        camera capabilities and extended battery life. The new model includes a revolutionary
        chip designed specifically for on-device machine learning tasks, enabling faster image
        processing without relying on cloud services. The camera system uses computational
        photography techniques to enhance low-light performance and add professional-grade
        portrait effects. Battery improvements promise up to two days of typical use on a
        single charge. The device also introduces innovative gesture controls and enhanced
        privacy features that give users more control over app permissions and data sharing.
        Pre-orders begin next week with shipping expected before the holiday season. Analysts
        predict strong sales driven by consumers upgrading from older models and the growing
        demand for AI-powered features.
        """,
        "Sports_News": """
        In a thrilling championship match, the underdog team secured victory in the final
        minutes with a spectacular goal that sent fans into celebration. The tournament's
        leading scorer delivered a clutch performance, demonstrating exceptional skill under
        pressure. This historic win marks the team's first championship in over two decades,
        ending a long drought that had frustrated supporters. The team's coach credited the
        victory to months of intensive training and strategic game planning. Player interviews
        revealed the emotional journey and determination that fueled their success. Celebration
        erupted in the team's home city as thousands gathered to watch the match on giant
        screens. The victory parade is scheduled for this weekend, expecting hundreds of
        thousands of attendees. Sports analysts are already discussing how this win could
        impact next season's dynamics and player recruitment.
        """,
        "Health_News": """
        Medical researchers publish groundbreaking study showing promising results for new
        treatment approach to combat antibiotic-resistant bacteria. The clinical trial involved
        over 500 patients across multiple hospitals and demonstrated significantly improved
        outcomes compared to conventional antibiotics. The novel therapeutic strategy uses
        bacteriophages, viruses that specifically target harmful bacteria without affecting
        beneficial microbiome populations. This advancement could address the growing global
        health crisis of antibiotic resistance, which claims hundreds of thousands of lives
        annually. The research team plans to expand trials and work toward regulatory approval
        within the next two years. Public health officials express cautious optimism about
        the potential to save lives and reduce healthcare costs. The pharmaceutical company
        funding the research saw stock increases as investors recognized the market potential
        for effective antibacterial treatments.
        """,
        #     "Quantum_Computing": """
        # Quantum computing leverages the principles of quantum mechanics to perform
        # calculations far beyond the reach of classical computers. Quantum bits, or qubits,
        # can exist in superposition, allowing simultaneous computation of multiple states.
        # Entanglement enables qubits to share information instantly over distance, providing
        # exponential speedup for certain algorithms. Applications include cryptography,
        # optimization, drug discovery, and materials science. Researchers are developing
        # quantum error correction methods and scalable hardware to make practical quantum
        # computers a reality. Companies like IBM, Google, and Rigetti are pioneering
        # experimental quantum processors, pushing the boundaries of what computing can achieve.
        # """,
        #     "Quantum_Physics": """
        # Quantum physics studies the behavior of matter and energy at the smallest scales,
        # where classical mechanics no longer applies. Concepts like wave-particle duality,
        # superposition, and entanglement describe how particles interact in ways that
        # defy intuition. Experiments such as the double-slit experiment and Bell’s theorem
        # tests confirm these principles, which form the foundation for technologies like
        # semiconductors, lasers, and quantum computers. Quantum theory continues to challenge
        # our understanding of reality, leading to new insights in fields ranging from
        # condensed matter physics to cosmology.
        # """,
        #     "Medieval_History": """
        # Medieval history covers roughly the 5th to 15th centuries in Europe, a period
        # marked by feudalism, the rise of kingdoms, and the influence of the Catholic
        # Church. Significant events include the Crusades, the Black Death, and the
        # Hundred Years’ War. Castles and fortified cities were built for protection,
        # while trade routes connected Europe with Asia and the Middle East. Art, literature,
        # and philosophy flourished under the patronage of nobles and the church, giving
        # rise to Gothic architecture and scholasticism. Monarchs and knights played
        # central roles in governance and military campaigns, and social hierarchies were
        # rigidly structured. Understanding medieval history helps explain the foundations
        # of modern European institutions, culture, and political systems.
        # """,
        #     "Modern_Art": """
        # Modern art emerged in the late 19th and early 20th centuries, breaking with
        # traditional forms and exploring new approaches to creativity and expression.
        # Movements such as Impressionism, Cubism, Surrealism, and Abstract Expressionism
        # challenged conventional techniques, emphasizing individual perspective,
        # experimentation with color, form, and media, and the exploration of psychological
        # and social themes. Artists like Picasso, Kandinsky, and Dali redefined the
        # boundaries of artistic practice. Modern art also reflects broader cultural
        # shifts, including industrialization, urbanization, and changing social norms.
        # Museums, galleries, and private collections showcase these works, inviting
        # audiences to engage with art in innovative and thought-provoking ways.
        # """,
    }

    asyncio.run(compare_embeddings(texts))
